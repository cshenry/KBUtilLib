"""Tests for kbutillib.cli.dropfile_emitter — atomic JSON drop-file writer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kbutillib.cli.dropfile_emitter import emit_session_dropfile


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_aia_root(tmp_path: Path) -> Path:
    """Create a minimal fake AIAssistant root (no sessions.db needed here)."""
    aia_root = tmp_path / "AIAssistant"
    aia_root.mkdir()
    return aia_root


def _minimal_payload(session_id: str = "abcd1234") -> dict:
    return {
        "session_id": session_id,
        "project_id": "modelingloe",
        "command": "kbu-plan",
        "summary": "test summary",
        "started_at": "2026-06-21T10:00:00Z",
    }


# ── emit_session_dropfile ──────────────────────────────────────────────────────


class TestEmitSessionDropfile:
    def test_creates_dropfile_in_inbox(self, tmp_path: Path) -> None:
        """emit_session_dropfile creates a JSON file in state/session-inbox/."""
        aia_root = _make_aia_root(tmp_path)
        payload = _minimal_payload("abc12345")
        path = emit_session_dropfile(aia_root, payload)

        assert path.exists()
        assert path.parent == aia_root / "state" / "session-inbox"
        assert path.name == "abc12345.json"

    def test_creates_inbox_dir_on_demand(self, tmp_path: Path) -> None:
        """Inbox directory is created if it doesn't exist yet."""
        aia_root = _make_aia_root(tmp_path)
        inbox = aia_root / "state" / "session-inbox"
        assert not inbox.exists()

        emit_session_dropfile(aia_root, _minimal_payload())
        assert inbox.is_dir()

    def test_schema_complete_json_roundtrip(self, tmp_path: Path) -> None:
        """Drop-file is valid JSON that round-trips back to an equal dict."""
        aia_root = _make_aia_root(tmp_path)
        payload = {
            "session_id": "feed0001",
            "project_id": "myproject",
            "project_name": "My Project",
            "command": "kbu-synthesize",
            "summary": "synthesis complete",
            "started_at": "2026-06-21T08:00:00Z",
            "ended_at": "2026-06-21T09:30:00Z",
            "topics_discussed": ["topic A", "topic B"],
            "decisions_made": ["decision 1"],
            "work_submitted": ["PR #42"],
            "next_steps": ["next step"],
        }
        path = emit_session_dropfile(aia_root, payload)

        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)

        assert loaded["session_id"] == "feed0001"
        assert loaded["project_id"] == "myproject"
        assert loaded["project_name"] == "My Project"
        assert loaded["command"] == "kbu-synthesize"
        assert loaded["summary"] == "synthesis complete"
        assert loaded["started_at"] == "2026-06-21T08:00:00Z"
        assert loaded["ended_at"] == "2026-06-21T09:30:00Z"
        assert loaded["topics_discussed"] == ["topic A", "topic B"]
        assert loaded["decisions_made"] == ["decision 1"]
        assert loaded["work_submitted"] == ["PR #42"]
        assert loaded["next_steps"] == ["next step"]

    def test_filename_is_session_id(self, tmp_path: Path) -> None:
        """Drop-file is named <session_id>.json."""
        aia_root = _make_aia_root(tmp_path)
        path = emit_session_dropfile(aia_root, _minimal_payload("deadbeef"))
        assert path.stem == "deadbeef"
        assert path.suffix == ".json"

    def test_missing_required_field_raises_value_error(self, tmp_path: Path) -> None:
        """ValueError raised if any required field is absent."""
        aia_root = _make_aia_root(tmp_path)
        for missing_field in ("session_id", "project_id", "command", "summary", "started_at"):
            payload = _minimal_payload()
            del payload[missing_field]
            with pytest.raises(ValueError, match="missing required fields"):
                emit_session_dropfile(aia_root, payload)

    def test_project_name_defaults_to_project_id(self, tmp_path: Path) -> None:
        """When project_name is absent, it defaults to project_id."""
        aia_root = _make_aia_root(tmp_path)
        payload = _minimal_payload()
        # No project_name in payload
        path = emit_session_dropfile(aia_root, payload)
        with open(path) as fh:
            loaded = json.load(fh)
        assert loaded["project_name"] == payload["project_id"]

    def test_optional_list_fields_default_to_empty(self, tmp_path: Path) -> None:
        """Optional list fields (topics_discussed etc.) default to [] when absent."""
        aia_root = _make_aia_root(tmp_path)
        path = emit_session_dropfile(aia_root, _minimal_payload())
        with open(path) as fh:
            loaded = json.load(fh)
        assert loaded["topics_discussed"] == []
        assert loaded["decisions_made"] == []
        assert loaded["work_submitted"] == []
        assert loaded["next_steps"] == []

    def test_ended_at_included_when_present(self, tmp_path: Path) -> None:
        """ended_at is included in the drop-file when provided."""
        aia_root = _make_aia_root(tmp_path)
        payload = _minimal_payload()
        payload["ended_at"] = "2026-06-21T11:00:00Z"
        path = emit_session_dropfile(aia_root, payload)
        with open(path) as fh:
            loaded = json.load(fh)
        assert loaded["ended_at"] == "2026-06-21T11:00:00Z"

    def test_ended_at_absent_when_not_in_payload(self, tmp_path: Path) -> None:
        """ended_at is omitted from drop-file when not in payload."""
        aia_root = _make_aia_root(tmp_path)
        path = emit_session_dropfile(aia_root, _minimal_payload())
        with open(path) as fh:
            loaded = json.load(fh)
        assert "ended_at" not in loaded

    def test_write_is_atomic_no_partial_file(self, tmp_path: Path) -> None:
        """No .tmp files are left in the inbox after a successful write."""
        aia_root = _make_aia_root(tmp_path)
        emit_session_dropfile(aia_root, _minimal_payload())
        inbox = aia_root / "state" / "session-inbox"
        tmp_files = list(inbox.glob("*.tmp"))
        assert tmp_files == [], "Temp files should be cleaned up after atomic rename"

    def test_overwrite_existing_dropfile(self, tmp_path: Path) -> None:
        """Emitting with the same session_id overwrites the existing drop-file."""
        aia_root = _make_aia_root(tmp_path)
        payload1 = _minimal_payload("sameId00")
        payload1["summary"] = "first"
        emit_session_dropfile(aia_root, payload1)

        payload2 = _minimal_payload("sameId00")
        payload2["summary"] = "second"
        path = emit_session_dropfile(aia_root, payload2)

        with open(path) as fh:
            loaded = json.load(fh)
        assert loaded["summary"] == "second"

        # Exactly one file
        inbox = aia_root / "state" / "session-inbox"
        assert len(list(inbox.glob("*.json"))) == 1

    def test_returns_path_to_dropfile(self, tmp_path: Path) -> None:
        """emit_session_dropfile returns the Path to the written file."""
        aia_root = _make_aia_root(tmp_path)
        path = emit_session_dropfile(aia_root, _minimal_payload("ret12345"))
        assert isinstance(path, Path)
        assert path.exists()
