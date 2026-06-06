"""Tests for kbutillib.cli.session — save, list, show subcommands."""

from __future__ import annotations

import json
import os
import sys
import types
import uuid
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


# ── save — local fallback ─────────────────────────────────────────────────────


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


# ── save — AIAssistant routing ────────────────────────────────────────────────


class TestSaveAIA:
    def _setup_aia_mocks(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> tuple[Path, list]:
        """Set up a fake AIAssistant environment and return (aia_root, calls_log)."""
        state_dir = tmp_path / "AIAssistant" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "sessions.db").write_bytes(b"fake")

        calls_log: list[dict] = []

        def fake_save_session(payload: dict) -> str:
            calls_log.append(dict(payload))
            return payload.get("session_id", "fakeid12")

        def fake_get_recent_sessions(**kwargs: Any) -> list:
            return []

        fake_state = types.SimpleNamespace(
            save_session=fake_save_session,
            get_recent_sessions=fake_get_recent_sessions,
        )

        fake_registry = types.SimpleNamespace(
            update_project=lambda *a, **kw: None,
        )

        monkeypatch.setitem(sys.modules, "assistant", types.ModuleType("assistant"))
        monkeypatch.setitem(sys.modules, "assistant.state", fake_state)
        monkeypatch.setitem(sys.modules, "assistant.state.registry", fake_registry)
        monkeypatch.setenv("KBU_AIA_PATHS", str(state_dir / "sessions.db"))

        return tmp_path / "AIAssistant", calls_log

    def test_save_session_called_with_correct_payload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        aia_root, calls_log = self._setup_aia_mocks(monkeypatch, tmp_path)
        root = _make_project(tmp_path)
        _create_subproject(root, "bar")
        result = _invoke(
            root, "save",
            "--skill", "kbu-build",
            "--subproject", "bar",
            "--summary", "scaffold done",
        )
        assert result.exit_code == 0, result.output
        assert len(calls_log) == 1
        payload = calls_log[0]
        assert payload["command"] == "kbu-build"
        assert payload["summary"] == "scaffold done"
        assert "project_id" in payload
        assert payload["project_id"].endswith("-bar")

    def test_project_id_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        aia_root, calls_log = self._setup_aia_mocks(monkeypatch, tmp_path)
        root = _make_project(tmp_path, name="testproj")
        _create_subproject(root, "mysp")
        _invoke(
            root, "save",
            "--skill", "kbu-plan",
            "--subproject", "mysp",
            "--summary", "s",
        )
        assert len(calls_log) == 1
        # project_id should be kbu-<cwd_basename>-mysp
        pid = calls_log[0]["project_id"]
        assert pid.endswith("-mysp")
        assert pid.startswith("kbu-")

    def test_importerror_falls_back_to_local(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If assistant.state raises ImportError, fall back to local YAML silently."""
        state_dir = tmp_path / "AIAssistant" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "sessions.db").write_bytes(b"fake")

        # Make assistant.state importable but save_session is absent — simulate
        # ImportError by blocking the module entirely.
        bad_module = types.ModuleType("assistant.state")

        def broken_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "assistant.state":
                raise ImportError("simulated missing assistant.state")
            return original_import(name, *args, **kwargs)

        import builtins
        original_import = builtins.__import__

        monkeypatch.setenv("KBU_AIA_PATHS", str(state_dir / "sessions.db"))
        # Ensure assistant.state is NOT in sys.modules so import will be attempted
        monkeypatch.delitem(sys.modules, "assistant.state", raising=False)
        monkeypatch.delitem(sys.modules, "assistant", raising=False)
        monkeypatch.setattr(builtins, "__import__", broken_import)

        root = _make_project(tmp_path)
        _create_subproject(root, "sp1")
        result = _invoke(root, "save", "--skill", "kbu-plan", "--subproject", "sp1",
                         "--summary", "test fallback")
        # Should still succeed
        assert result.exit_code == 0, result.output
        # Warning on stderr (captured in CliRunner output)
        assert "Warning" in result.output or "falling back" in result.output
        # Local YAML should exist
        sessions_dir = root / "subprojects" / "sp1" / "sessions"
        assert len(list(sessions_dir.glob("*.yaml"))) == 1

    def test_update_project_importerror_skips_registration_saves_anyway(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ImportError on update_project is non-fatal; save_session still called."""
        state_dir = tmp_path / "AIAssistant" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "sessions.db").write_bytes(b"fake")

        calls_log: list[dict] = []

        def fake_save_session(payload: dict) -> str:
            calls_log.append(dict(payload))
            return payload.get("session_id", "deadbeef")

        fake_state = types.SimpleNamespace(
            save_session=fake_save_session,
            get_recent_sessions=lambda **kw: [],
        )
        # Registry module present but update_project attribute missing
        fake_registry = types.SimpleNamespace()  # no update_project

        monkeypatch.setitem(sys.modules, "assistant", types.ModuleType("assistant"))
        monkeypatch.setitem(sys.modules, "assistant.state", fake_state)
        monkeypatch.setitem(sys.modules, "assistant.state.registry", fake_registry)
        monkeypatch.setenv("KBU_AIA_PATHS", str(state_dir / "sessions.db"))

        root = _make_project(tmp_path)
        _create_subproject(root, "sp2")
        result = _invoke(
            root, "save",
            "--skill", "kbu-run",
            "--subproject", "sp2",
            "--summary", "ran notebooks",
        )
        assert result.exit_code == 0, result.output
        assert len(calls_log) == 1
        assert calls_log[0]["command"] == "kbu-run"


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
