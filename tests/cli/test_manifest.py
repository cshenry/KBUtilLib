"""Tests for kbutillib.cli.manifest — shared TOML I/O helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kbutillib.cli.manifest import (
    append_notebook_entry_or_update,
    append_session_ref,
    now_utc_iso,
    read_project_manifest,
    read_subproject_manifest,
    sha256_file,
    write_project_manifest,
    write_subproject_manifest,
)


# ── now_utc_iso ─────────────────────────────────────────────────────────────


class TestNowUtcIso:
    def test_ends_with_z(self) -> None:
        ts = now_utc_iso()
        assert ts.endswith("Z")

    def test_is_iso_format(self) -> None:
        ts = now_utc_iso()
        # Must be parseable as ISO-8601 (strip Z, replace with +00:00)
        from datetime import datetime
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert dt.tzinfo is not None

    def test_format_matches_pattern(self) -> None:
        import re
        ts = now_utc_iso()
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", ts)


# ── sha256_file ──────────────────────────────────────────────────────────────


class TestSha256File:
    def test_matches_stdlib(self, tmp_path: Path) -> None:
        f = tmp_path / "data.bin"
        f.write_bytes(b"hello world\n")
        expected = hashlib.sha256(b"hello world\n").hexdigest()
        assert sha256_file(f) == expected

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        assert sha256_file(f) == hashlib.sha256(b"").hexdigest()

    def test_large_file(self, tmp_path: Path) -> None:
        f = tmp_path / "big.bin"
        data = b"x" * (200 * 1024)  # 200 KiB
        f.write_bytes(data)
        assert sha256_file(f) == hashlib.sha256(data).hexdigest()


# ── project manifest round-trip ──────────────────────────────────────────────


class TestProjectManifestRoundTrip:
    def _sample_data(self) -> dict:
        now = now_utc_iso()
        return {
            "project": {
                "name": "henry_lab",
                "title": "Henry Lab workspace",
                "created_at": now,
                "authors": [
                    {
                        "name": "Chris Henry",
                        "affiliation": "ANL",
                        "orcid": "0000-0001-9999-0000",
                    }
                ],
            },
            "kbutillib": {
                "source_path": "/Dropbox/Projects/KBUtilLib",
                "source_commit": "deadbeef",
            },
            "update": {
                "last_pulled_at": now,
                "last_pulled_commit": "deadbeef",
            },
        }

    def test_round_trip_preserves_all_fields(self, tmp_path: Path) -> None:
        data = self._sample_data()
        write_project_manifest(tmp_path, data)
        read_back = read_project_manifest(tmp_path)
        assert read_back["project"]["name"] == "henry_lab"
        assert read_back["project"]["title"] == "Henry Lab workspace"
        assert read_back["project"]["authors"][0]["name"] == "Chris Henry"
        assert read_back["kbutillib"]["source_path"] == "/Dropbox/Projects/KBUtilLib"
        assert read_back["update"]["last_pulled_commit"] == "deadbeef"

    def test_write_creates_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "new_project"
        assert not root.exists()
        write_project_manifest(root, self._sample_data())
        assert root.is_dir()
        assert (root / "kbu-project.toml").exists()

    def test_read_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read_project_manifest(tmp_path / "nonexistent")

    def test_overwrite_preserves_correct_values(self, tmp_path: Path) -> None:
        data = self._sample_data()
        write_project_manifest(tmp_path, data)
        data["kbutillib"]["source_commit"] = "newcommit"
        write_project_manifest(tmp_path, data)
        read_back = read_project_manifest(tmp_path)
        assert read_back["kbutillib"]["source_commit"] == "newcommit"


# ── subproject manifest round-trip ───────────────────────────────────────────


class TestSubprojectManifestRoundTrip:
    def _sample_data(self, name: str = "test_sp") -> dict:
        now = now_utc_iso()
        return {
            "subproject": {
                "name": name,
                "title": "Test analysis",
                "status": "plan",
                "created_at": now,
                "last_session_at": now,
            },
            "artifacts": {
                "research_plan": False,
                "report": False,
                "reviews": {
                    "plan": [],
                    "build": [],
                    "synthesis": [],
                },
            },
            "notebooks": [],
            "session_refs": [],
        }

    def test_round_trip_all_fields(self, tmp_path: Path) -> None:
        data = self._sample_data()
        write_subproject_manifest(tmp_path, "test_sp", data)
        read_back = read_subproject_manifest(tmp_path, "test_sp")
        assert read_back["subproject"]["name"] == "test_sp"
        assert read_back["subproject"]["status"] == "plan"
        assert read_back["artifacts"]["research_plan"] is False
        assert read_back["artifacts"]["reviews"]["plan"] == []
        assert read_back["notebooks"] == []
        assert read_back["session_refs"] == []

    def test_no_artifacts_notebooks_key(self, tmp_path: Path) -> None:
        """[artifacts.notebooks] must not exist (round-2 schema correction)."""
        data = self._sample_data()
        write_subproject_manifest(tmp_path, "test_sp", data)
        read_back = read_subproject_manifest(tmp_path, "test_sp")
        # Confirm the artifacts section does not have a 'notebooks' key
        assert "notebooks" not in read_back.get("artifacts", {})

    def test_creates_subproject_dir(self, tmp_path: Path) -> None:
        sp_dir = tmp_path / "subprojects" / "mysp"
        assert not sp_dir.exists()
        write_subproject_manifest(tmp_path, "mysp", self._sample_data("mysp"))
        assert sp_dir.is_dir()
        assert (sp_dir / "kbu-subproject.toml").exists()

    def test_status_field_key_is_status(self, tmp_path: Path) -> None:
        """Manifest key must be [subproject].status, not [subproject].state."""
        data = self._sample_data()
        write_subproject_manifest(tmp_path, "test_sp", data)
        raw = (tmp_path / "subprojects" / "test_sp" / "kbu-subproject.toml").read_text()
        assert "status" in raw
        assert "state" not in raw  # Ensure 'state' key is absent

    def test_status_count_equals_one(self, tmp_path: Path) -> None:
        """grep -c 'status =' should return 1."""
        data = self._sample_data()
        write_subproject_manifest(tmp_path, "test_sp", data)
        raw = (tmp_path / "subprojects" / "test_sp" / "kbu-subproject.toml").read_text()
        count = sum(1 for line in raw.splitlines() if "status =" in line)
        assert count == 1


# ── append_session_ref ───────────────────────────────────────────────────────


class TestAppendSessionRef:
    def _create_base(self, tmp_path: Path, name: str = "sp1") -> None:
        now = now_utc_iso()
        data = {
            "subproject": {
                "name": name,
                "title": "test",
                "status": "plan",
                "created_at": now,
                "last_session_at": now,
            },
            "artifacts": {
                "research_plan": False,
                "report": False,
                "reviews": {"plan": [], "build": [], "synthesis": []},
            },
            "notebooks": [{"slug": "01", "last_run_at": now, "modified_since_run": False}],
            "session_refs": [],
        }
        write_subproject_manifest(tmp_path, name, data)

    def test_appends_ref(self, tmp_path: Path) -> None:
        self._create_base(tmp_path)
        ref = {"id": "abc12345", "skill": "kbu-plan", "at": now_utc_iso(), "summary": "Done"}
        append_session_ref(tmp_path, "sp1", ref)
        data = read_subproject_manifest(tmp_path, "sp1")
        assert len(data["session_refs"]) == 1
        assert data["session_refs"][0]["id"] == "abc12345"

    def test_preserves_other_fields(self, tmp_path: Path) -> None:
        """Appending a session ref must not drop other fields like notebooks."""
        self._create_base(tmp_path)
        ref = {"id": "x1", "skill": "kbu-plan", "at": now_utc_iso(), "summary": "s"}
        append_session_ref(tmp_path, "sp1", ref)
        data = read_subproject_manifest(tmp_path, "sp1")
        # notebooks should still be there
        assert len(data["notebooks"]) == 1
        assert data["notebooks"][0]["slug"] == "01"
        # subproject section should be intact
        assert data["subproject"]["status"] == "plan"

    def test_updates_last_session_at(self, tmp_path: Path) -> None:
        self._create_base(tmp_path)
        ts = now_utc_iso()
        ref = {"id": "y1", "skill": "kbu-build", "at": ts, "summary": "Built"}
        append_session_ref(tmp_path, "sp1", ref)
        data = read_subproject_manifest(tmp_path, "sp1")
        assert data["subproject"]["last_session_at"] == ts

    def test_multiple_appends(self, tmp_path: Path) -> None:
        self._create_base(tmp_path)
        for i in range(3):
            ref = {"id": f"ref{i}", "skill": "kbu-plan", "at": now_utc_iso(), "summary": f"s{i}"}
            append_session_ref(tmp_path, "sp1", ref)
        data = read_subproject_manifest(tmp_path, "sp1")
        assert len(data["session_refs"]) == 3


# ── append_notebook_entry_or_update ─────────────────────────────────────────


class TestAppendNotebookEntryOrUpdate:
    def _create_base(self, tmp_path: Path, name: str = "sp1") -> None:
        now = now_utc_iso()
        data = {
            "subproject": {
                "name": name,
                "title": "test",
                "status": "run",
                "created_at": now,
                "last_session_at": now,
            },
            "artifacts": {
                "research_plan": True,
                "report": False,
                "reviews": {"plan": ["REVIEW_plan_1.md"], "build": [], "synthesis": []},
            },
            "notebooks": [
                {
                    "slug": "01_explore",
                    "last_run_at": "2026-01-01T10:00:00Z",
                    "modified_since_run": True,
                }
            ],
            "session_refs": [{"id": "existingref", "skill": "kbu-plan", "at": now, "summary": "x"}],
        }
        write_subproject_manifest(tmp_path, name, data)

    def test_updates_existing_entry(self, tmp_path: Path) -> None:
        self._create_base(tmp_path)
        new_ts = now_utc_iso()
        append_notebook_entry_or_update(tmp_path, "sp1", "01_explore", new_ts)
        data = read_subproject_manifest(tmp_path, "sp1")
        nb = data["notebooks"][0]
        assert nb["slug"] == "01_explore"
        assert nb["last_run_at"] == new_ts
        assert nb["modified_since_run"] is False

    def test_appends_new_entry(self, tmp_path: Path) -> None:
        self._create_base(tmp_path)
        new_ts = now_utc_iso()
        append_notebook_entry_or_update(tmp_path, "sp1", "02_analysis", new_ts)
        data = read_subproject_manifest(tmp_path, "sp1")
        assert len(data["notebooks"]) == 2
        slugs = [nb["slug"] for nb in data["notebooks"]]
        assert "02_analysis" in slugs

    def test_preserves_other_notebooks_and_sessions(self, tmp_path: Path) -> None:
        self._create_base(tmp_path)
        new_ts = now_utc_iso()
        append_notebook_entry_or_update(tmp_path, "sp1", "01_explore", new_ts)
        data = read_subproject_manifest(tmp_path, "sp1")
        # session_refs must still be there
        assert len(data["session_refs"]) == 1
        assert data["session_refs"][0]["id"] == "existingref"
        # artifacts untouched
        assert data["artifacts"]["research_plan"] is True
        assert data["artifacts"]["reviews"]["plan"] == ["REVIEW_plan_1.md"]

    def test_update_does_not_duplicate(self, tmp_path: Path) -> None:
        self._create_base(tmp_path)
        ts1 = now_utc_iso()
        append_notebook_entry_or_update(tmp_path, "sp1", "01_explore", ts1)
        ts2 = now_utc_iso()
        append_notebook_entry_or_update(tmp_path, "sp1", "01_explore", ts2)
        data = read_subproject_manifest(tmp_path, "sp1")
        # Still only one entry for the 01_explore slug
        count = sum(1 for nb in data["notebooks"] if nb["slug"] == "01_explore")
        assert count == 1
        assert data["notebooks"][0]["last_run_at"] == ts2
