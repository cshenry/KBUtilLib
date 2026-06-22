"""Tests for kbutillib.cli.session — save, list, show subcommands.

The old _route_save_aia direct save_session path has been removed.  Tests now
verify the new drop-file behaviour: bound project emits a drop-file AND writes
local YAML; unbound project writes ONLY local YAML and emits nothing; AIAssistant-
absent writes ONLY local YAML.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.cli.manifest import (
    now_utc_iso,
    read_subproject_manifest,
    write_project_manifest,
    write_subproject_manifest,
)
from kbutillib.cli.session import _detect_aiassistant, _find_project_root


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_project(tmp_path: Path, name: str = "myproj") -> Path:
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    now = now_utc_iso()
    write_project_manifest(root, {
        "project": {"name": name, "title": name, "created_at": now},
        "kbutillib": {"source_path": "/fake", "source_commit": "abc"},
        "update": {"last_pulled_at": now, "last_pulled_commit": "abc"},
    })
    return root


def _create_subproject(root: Path, sp_name: str) -> Path:
    now = now_utc_iso()
    sp_dir = root / "subprojects" / sp_name
    sp_dir.mkdir(parents=True, exist_ok=True)
    (sp_dir / "sessions").mkdir(exist_ok=True)
    data: dict[str, Any] = {
        "subproject": {
            "name": sp_name,
            "title": sp_name,
            "status": "plan",
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


def _add_binding(root: Path, project_id: str, project_name: str) -> None:
    """Add an [aiassistant] binding to the project's kbu-project.toml."""
    from kbutillib.cli.binding import set_binding
    set_binding(root, project_id, project_name)


def _make_aia_root(tmp_path: Path, subdir: str = "AIAssistant") -> Path:
    """Create a fake AIAssistant root with state/sessions.db present."""
    aia_root = tmp_path / subdir
    state_dir = aia_root / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "sessions.db").write_bytes(b"fake")
    return aia_root


def _invoke(root: Path, *args: str) -> Any:
    runner = CliRunner()
    saved = os.getcwd()
    try:
        os.chdir(root)
        return runner.invoke(main, ["session", *args], catch_exceptions=False)
    finally:
        os.chdir(saved)


# ── _detect_aiassistant ───────────────────────────────────────────────────────


class TestDetectAiassistant:
    def test_returns_none_when_default_paths_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When both default db paths are absent and env var is not set, return None."""
        monkeypatch.delenv("KBU_AIA_PATHS", raising=False)
        # Ensure neither default path exists by pointing to a tmp nonexistent dir.
        monkeypatch.setenv("KBU_AIA_PATHS", "/nonexistent/a/state/sessions.db:/nonexistent/b/state/sessions.db")
        result = _detect_aiassistant()
        assert result is None

    def test_returns_none_when_env_nonexistent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KBU_AIA_PATHS", "/nonexistent/sessions.db")
        assert _detect_aiassistant() is None

    def test_returns_repo_root_when_db_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When sessions.db exists at the given path, return parent of state/."""
        state_dir = tmp_path / "AIAssistant" / "state"
        state_dir.mkdir(parents=True)
        db = state_dir / "sessions.db"
        db.write_bytes(b"")
        monkeypatch.setenv("KBU_AIA_PATHS", str(db))
        result = _detect_aiassistant()
        assert result == tmp_path / "AIAssistant"

    def test_picks_first_existing_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """First existing path wins; second is irrelevant."""
        a_state = tmp_path / "A" / "state"
        a_state.mkdir(parents=True)
        (a_state / "sessions.db").write_bytes(b"")
        b_state = tmp_path / "B" / "state"
        b_state.mkdir(parents=True)
        (b_state / "sessions.db").write_bytes(b"")
        monkeypatch.setenv(
            "KBU_AIA_PATHS",
            f"{a_state / 'sessions.db'}:{b_state / 'sessions.db'}",
        )
        result = _detect_aiassistant()
        assert result == tmp_path / "A"

    def test_env_override_custom_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """KBU_AIA_PATHS override with a custom path is respected."""
        state_dir = tmp_path / "custom_aia" / "state"
        state_dir.mkdir(parents=True)
        db = state_dir / "sessions.db"
        db.write_bytes(b"fake db")
        monkeypatch.setenv("KBU_AIA_PATHS", str(db))
        result = _detect_aiassistant()
        assert result == tmp_path / "custom_aia"


# ── save — local YAML (no AIAssistant) ───────────────────────────────────────


class TestSaveLocal:
    def test_writes_yaml_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With KBU_AIA_PATHS pointing to nonexistent db, writes local YAML."""
        monkeypatch.setenv("KBU_AIA_PATHS", "/nonexistent/state/sessions.db")
        root = _make_project(tmp_path)
        _create_subproject(root, "foo")
        result = _invoke(root, "save", "--skill", "kbu-plan", "--subproject", "foo",
                         "--summary", "hello")
        assert result.exit_code == 0, result.output

        sessions_dir = root / "subprojects" / "foo" / "sessions"
        yaml_files = list(sessions_dir.glob("*.yaml"))
        assert len(yaml_files) == 1

        with open(yaml_files[0]) as fh:
            data = yaml.safe_load(fh)
        assert data["summary"] == "hello"
        assert data["command"] == "kbu-plan"

    def test_session_id_8hex(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """session_id is 8 hex characters."""
        monkeypatch.setenv("KBU_AIA_PATHS", "/nonexistent/state/sessions.db")
        root = _make_project(tmp_path)
        _create_subproject(root, "foo")
        result = _invoke(root, "save", "--skill", "kbu-plan", "--subproject", "foo",
                         "--summary", "hi")
        assert result.exit_code == 0, result.output
        sid = result.output.strip()
        assert len(sid) == 8
        int(sid, 16)  # must be valid hex

    def test_updates_subproject_manifest_session_refs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KBU_AIA_PATHS", "/nonexistent/state/sessions.db")
        root = _make_project(tmp_path)
        _create_subproject(root, "foo")
        result = _invoke(root, "save", "--skill", "kbu-plan", "--subproject", "foo",
                         "--summary", "done")
        assert result.exit_code == 0, result.output
        data = read_subproject_manifest(root, "foo")
        assert len(data["session_refs"]) == 1
        ref = data["session_refs"][0]
        assert ref["skill"] == "kbu-plan"
        assert ref["summary"] == "done"

    def test_with_optional_fields(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KBU_AIA_PATHS", "/nonexistent/state/sessions.db")
        root = _make_project(tmp_path)
        _create_subproject(root, "foo")
        result = _invoke(
            root, "save",
            "--skill", "kbu-plan",
            "--subproject", "foo",
            "--summary", "finished",
            "--topics", "topic1",
            "--decisions", "decision1",
            "--next-steps", "step1",
            "--work-completed", "work1",
        )
        assert result.exit_code == 0, result.output
        sessions_dir = root / "subprojects" / "foo" / "sessions"
        yf = next(sessions_dir.glob("*.yaml"))
        with open(yf) as fh:
            data = yaml.safe_load(fh)
        assert data["topics_discussed"] == ["topic1"]
        assert data["decisions_made"] == ["decision1"]
        assert data["next_steps"] == ["step1"]
        assert data["work_submitted"] == ["work1"]

    def test_no_dropfile_when_aia_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When AIAssistant is absent, no drop-file is created anywhere."""
        monkeypatch.setenv("KBU_AIA_PATHS", "/nonexistent/state/sessions.db")
        root = _make_project(tmp_path)
        _create_subproject(root, "sp1")
        result = _invoke(root, "save", "--skill", "kbu-plan", "--subproject", "sp1",
                         "--summary", "no aia")
        assert result.exit_code == 0, result.output
        # No session-inbox directory should exist anywhere under tmp_path
        inboxes = list(tmp_path.rglob("session-inbox"))
        assert inboxes == []


# ── save — drop-file routing (AIAssistant present) ───────────────────────────


class TestSaveDropfile:
    def _setup_aia(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, Path]:
        """Return (project_root, aia_root) with AIA detected."""
        aia_root = _make_aia_root(tmp_path, "AIAssistant")
        monkeypatch.setenv("KBU_AIA_PATHS", str(aia_root / "state" / "sessions.db"))
        root = _make_project(tmp_path)
        return root, aia_root

    def test_bound_project_emits_dropfile_and_writes_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bound project: local YAML written AND drop-file emitted."""
        root, aia_root = self._setup_aia(tmp_path, monkeypatch)
        _create_subproject(root, "sp1")
        _add_binding(root, "modelingloe", "ModelingLOE")

        result = _invoke(root, "save", "--skill", "kbu-plan", "--subproject", "sp1",
                         "--summary", "bound session")
        assert result.exit_code == 0, result.output

        # Local YAML must exist
        sessions_dir = root / "subprojects" / "sp1" / "sessions"
        yaml_files = list(sessions_dir.glob("*.yaml"))
        assert len(yaml_files) == 1, "Expected exactly one local YAML file"

        # Drop-file must exist
        inbox = aia_root / "state" / "session-inbox"
        drop_files = list(inbox.glob("*.json"))
        assert len(drop_files) == 1, "Expected exactly one drop-file"

        # Drop-file must be valid JSON with required fields
        with open(drop_files[0]) as fh:
            drop = json.load(fh)
        assert drop["project_id"] == "modelingloe"
        assert drop["project_name"] == "ModelingLOE"
        assert drop["command"] == "kbu-plan"
        assert drop["summary"] == "bound session"
        assert "session_id" in drop
        assert "started_at" in drop

        # Drop-file name must match session_id
        assert drop_files[0].stem == drop["session_id"]

    def test_unbound_project_writes_only_local_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unbound project with AIAssistant present: local YAML only, no drop-file."""
        root, aia_root = self._setup_aia(tmp_path, monkeypatch)
        _create_subproject(root, "sp1")
        # No binding added

        result = _invoke(root, "save", "--skill", "kbu-build", "--subproject", "sp1",
                         "--summary", "unbound session")
        assert result.exit_code == 0, result.output

        # Local YAML must exist
        sessions_dir = root / "subprojects" / "sp1" / "sessions"
        yaml_files = list(sessions_dir.glob("*.yaml"))
        assert len(yaml_files) == 1, "Expected exactly one local YAML file"

        # No drop-file should be created
        inbox = aia_root / "state" / "session-inbox"
        if inbox.exists():
            drop_files = list(inbox.glob("*.json"))
            assert drop_files == [], "Expected no drop-files for unbound project"

    def test_aia_absent_writes_only_local_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AIAssistant absent (no sessions.db): local YAML only, no drop-file."""
        monkeypatch.setenv("KBU_AIA_PATHS", "/nonexistent/state/sessions.db")
        root = _make_project(tmp_path)
        _create_subproject(root, "sp1")
        # Even if there's a binding, AIAssistant isn't detected
        _add_binding(root, "modelingloe", "ModelingLOE")

        result = _invoke(root, "save", "--skill", "kbu-run", "--subproject", "sp1",
                         "--summary", "no aia")
        assert result.exit_code == 0, result.output

        sessions_dir = root / "subprojects" / "sp1" / "sessions"
        yaml_files = list(sessions_dir.glob("*.yaml"))
        assert len(yaml_files) == 1

        inboxes = list(tmp_path.rglob("session-inbox"))
        assert inboxes == [], "No session-inbox should be created when AIA absent"

    def test_dropfile_contains_list_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drop-file carries topics/decisions/work/next_steps lists."""
        root, aia_root = self._setup_aia(tmp_path, monkeypatch)
        _create_subproject(root, "sp1")
        _add_binding(root, "testproject", "Test Project")

        result = _invoke(
            root, "save",
            "--skill", "kbu-synthesize",
            "--subproject", "sp1",
            "--summary", "synthesis done",
            "--topics", "topic A",
            "--decisions", "decision X",
            "--next-steps", "next step 1",
            "--work-completed", "delivered report",
        )
        assert result.exit_code == 0, result.output

        inbox = aia_root / "state" / "session-inbox"
        drop_files = list(inbox.glob("*.json"))
        assert len(drop_files) == 1

        with open(drop_files[0]) as fh:
            drop = json.load(fh)
        assert drop["topics_discussed"] == ["topic A"]
        assert drop["decisions_made"] == ["decision X"]
        assert drop["next_steps"] == ["next step 1"]
        assert drop["work_submitted"] == ["delivered report"]

    def test_dropfile_session_id_matches_stdout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The session_id printed to stdout matches the drop-file filename and content."""
        root, aia_root = self._setup_aia(tmp_path, monkeypatch)
        _create_subproject(root, "sp1")
        _add_binding(root, "proj1", "Proj One")

        result = _invoke(root, "save", "--skill", "kbu-plan", "--subproject", "sp1",
                         "--summary", "id check")
        assert result.exit_code == 0, result.output

        session_id = result.output.strip()
        inbox = aia_root / "state" / "session-inbox"
        drop_file = inbox / f"{session_id}.json"
        assert drop_file.exists(), f"Expected drop-file at {drop_file}"

        with open(drop_file) as fh:
            drop = json.load(fh)
        assert drop["session_id"] == session_id

    def test_no_assistant_state_warning_on_save(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Saving must NOT produce the old 'could not import assistant.state' warning."""
        root, aia_root = self._setup_aia(tmp_path, monkeypatch)
        _create_subproject(root, "sp1")
        _add_binding(root, "proj1", "Proj")

        result = _invoke(root, "save", "--skill", "kbu-plan", "--subproject", "sp1",
                         "--summary", "import check")
        assert result.exit_code == 0, result.output
        # Old error path would have printed these strings
        assert "could not import assistant.state" not in result.output
        assert "falling back to local YAML" not in result.output
        # And the old auto-register warning should not appear
        assert "auto-register" not in result.output


# ── list ──────────────────────────────────────────────────────────────────────


class TestList:
    def test_tsv_header_row(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KBU_AIA_PATHS", "/nonexistent/state/sessions.db")
        root = _make_project(tmp_path)
        result = _invoke(root, "list")
        assert result.exit_code == 0, result.output
        first_line = result.output.splitlines()[0]
        assert first_line == "id\tat\tsubproject\tskill\tsummary"

    def test_shows_saved_session(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KBU_AIA_PATHS", "/nonexistent/state/sessions.db")
        root = _make_project(tmp_path)
        _create_subproject(root, "sp1")
        _invoke(root, "save", "--skill", "kbu-plan", "--subproject", "sp1",
                "--summary", "planning done")
        result = _invoke(root, "list")
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert len(lines) == 2  # header + 1 row
        assert "kbu-plan" in lines[1]
        assert "planning done" in lines[1]

    def test_summary_truncation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KBU_AIA_PATHS", "/nonexistent/state/sessions.db")
        root = _make_project(tmp_path)
        _create_subproject(root, "sp1")
        long_summary = "x" * 200
        _invoke(root, "save", "--skill", "kbu-plan", "--subproject", "sp1",
                "--summary", long_summary)
        result = _invoke(root, "list")
        lines = result.output.splitlines()
        summary_col = lines[1].split("\t")[4]
        assert len(summary_col) <= 121  # 120 chars + possible ellipsis char
        assert summary_col.endswith("…")

    def test_summary_collapses_tabs_newlines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KBU_AIA_PATHS", "/nonexistent/state/sessions.db")
        root = _make_project(tmp_path)
        _create_subproject(root, "sp1")
        # Write a YAML file directly with embedded tabs/newlines in summary.
        sessions_dir = root / "subprojects" / "sp1" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        now = now_utc_iso()
        payload = {
            "session_id": "abc12345",
            "command": "kbu-plan",
            "summary": "line one\nline two\there",
            "started_at": now,
        }
        with open(sessions_dir / f"{now.replace(':', '')}-kbu-plan.yaml", "w") as fh:
            yaml.safe_dump(payload, fh)
        result = _invoke(root, "list")
        lines = result.output.splitlines()
        summary_col = lines[1].split("\t")[4]
        assert "\n" not in summary_col
        assert "\t" not in summary_col

    def test_filter_by_subproject(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KBU_AIA_PATHS", "/nonexistent/state/sessions.db")
        root = _make_project(tmp_path)
        _create_subproject(root, "sp1")
        _create_subproject(root, "sp2")
        _invoke(root, "save", "--skill", "kbu-plan", "--subproject", "sp1",
                "--summary", "sp1 session")
        _invoke(root, "save", "--skill", "kbu-build", "--subproject", "sp2",
                "--summary", "sp2 session")
        result = _invoke(root, "list", "--subproject", "sp1")
        assert result.exit_code == 0, result.output
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        # header + 1 data row only
        assert len(lines) == 2
        assert "sp1 session" in lines[1]
        assert "sp2 session" not in result.output

    def test_json_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KBU_AIA_PATHS", "/nonexistent/state/sessions.db")
        root = _make_project(tmp_path)
        _create_subproject(root, "sp1")
        _invoke(root, "save", "--skill", "kbu-plan", "--subproject", "sp1",
                "--summary", "json test")
        result = _invoke(root, "list", "--json")
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["summary"] == "json test"

    def test_limit(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KBU_AIA_PATHS", "/nonexistent/state/sessions.db")
        root = _make_project(tmp_path)
        _create_subproject(root, "sp1")
        for i in range(5):
            _invoke(root, "save", "--skill", "kbu-plan", "--subproject", "sp1",
                    "--summary", f"session {i}")
        result = _invoke(root, "list", "--limit", "3")
        assert result.exit_code == 0, result.output
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 4  # header + 3 rows

    def test_empty_project_just_header(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KBU_AIA_PATHS", "/nonexistent/state/sessions.db")
        root = _make_project(tmp_path)
        result = _invoke(root, "list")
        assert result.exit_code == 0
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert lines[0] == "id\tat\tsubproject\tskill\tsummary"


# ── show ──────────────────────────────────────────────────────────────────────


class TestShow:
    def test_shows_existing_session(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KBU_AIA_PATHS", "/nonexistent/state/sessions.db")
        root = _make_project(tmp_path)
        _create_subproject(root, "sp1")
        save_result = _invoke(root, "save", "--skill", "kbu-plan", "--subproject", "sp1",
                              "--summary", "show me")
        sid = save_result.output.strip()
        result = _invoke(root, "show", sid)
        assert result.exit_code == 0, result.output
        assert "show me" in result.output
        assert sid in result.output

    def test_nonexistent_session_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KBU_AIA_PATHS", "/nonexistent/state/sessions.db")
        root = _make_project(tmp_path)
        result = _invoke(root, "show", "deadbeef")
        assert result.exit_code != 0


# ── help / registration ───────────────────────────────────────────────────────


class TestHelp:
    def test_session_help_lists_commands(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["session", "--help"], catch_exceptions=False)
        assert result.exit_code == 0
        for cmd in ("save", "list", "show"):
            assert cmd in result.output

    def test_top_level_help_lists_session(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "session" in result.output

    def test_top_level_help_lists_set(self) -> None:
        """The new 'set' command group is registered at the top level."""
        runner = CliRunner()
        result = runner.invoke(main, ["--help"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "set" in result.output
