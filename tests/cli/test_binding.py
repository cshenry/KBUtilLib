"""Tests for kbutillib.cli.binding — project-to-AIAssistant project_id binding."""

from __future__ import annotations

from pathlib import Path

import pytest

from kbutillib.cli.binding import resolve_binding, set_binding
from kbutillib.cli.manifest import now_utc_iso, write_project_manifest


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_project(root: Path, name: str = "myproj") -> None:
    """Write a minimal kbu-project.toml into *root*."""
    now = now_utc_iso()
    write_project_manifest(root, {
        "project": {"name": name, "title": name, "created_at": now},
        "kbutillib": {"source_path": "/fake", "source_commit": "abc"},
        "update": {"last_pulled_at": now, "last_pulled_commit": "abc"},
    })


# ── resolve_binding ────────────────────────────────────────────────────────────


class TestResolveBinding:
    def test_returns_none_when_no_manifest(self, tmp_path: Path) -> None:
        """resolve_binding returns None when kbu-project.toml is absent."""
        result = resolve_binding(tmp_path / "nonexistent")
        assert result is None

    def test_returns_none_when_no_aiassistant_table(self, tmp_path: Path) -> None:
        """resolve_binding returns None when manifest has no [aiassistant] table."""
        root = tmp_path / "proj"
        root.mkdir()
        _make_project(root)
        result = resolve_binding(root)
        assert result is None

    def test_returns_none_when_project_id_empty(self, tmp_path: Path) -> None:
        """resolve_binding returns None when project_id is empty string."""
        root = tmp_path / "proj"
        root.mkdir()
        now = now_utc_iso()
        write_project_manifest(root, {
            "project": {"name": "p", "title": "p", "created_at": now},
            "kbutillib": {"source_path": "/f", "source_commit": "a"},
            "update": {"last_pulled_at": now, "last_pulled_commit": "a"},
            "aiassistant": {"project_id": "", "project_name": "P"},
        })
        result = resolve_binding(root)
        assert result is None

    def test_returns_project_id_when_set(self, tmp_path: Path) -> None:
        """resolve_binding returns the stored project_id."""
        root = tmp_path / "proj"
        root.mkdir()
        _make_project(root)
        set_binding(root, "modelingloe", "ModelingLOE")
        result = resolve_binding(root)
        assert result == "modelingloe"


# ── set_binding ────────────────────────────────────────────────────────────────


class TestSetBinding:
    def test_round_trips_project_id_and_name(self, tmp_path: Path) -> None:
        """set_binding then resolve_binding returns the stored project_id."""
        root = tmp_path / "proj"
        root.mkdir()
        _make_project(root)
        set_binding(root, "myproject", "My Project")
        assert resolve_binding(root) == "myproject"

    def test_preserves_existing_manifest_fields(self, tmp_path: Path) -> None:
        """set_binding does not clobber other manifest fields."""
        root = tmp_path / "proj"
        root.mkdir()
        _make_project(root)
        from kbutillib.cli.manifest import read_project_manifest
        before = read_project_manifest(root)
        set_binding(root, "testid", "Test")
        after = read_project_manifest(root)
        # Original fields must be preserved
        assert after["project"]["name"] == before["project"]["name"]
        assert after["kbutillib"]["source_commit"] == before["kbutillib"]["source_commit"]
        # New table is present
        assert after["aiassistant"]["project_id"] == "testid"
        assert after["aiassistant"]["project_name"] == "Test"

    def test_overwrites_existing_binding(self, tmp_path: Path) -> None:
        """set_binding called twice updates to the latest values."""
        root = tmp_path / "proj"
        root.mkdir()
        _make_project(root)
        set_binding(root, "first-id", "First")
        set_binding(root, "second-id", "Second")
        assert resolve_binding(root) == "second-id"

    def test_creates_manifest_if_absent(self, tmp_path: Path) -> None:
        """set_binding works even if no kbu-project.toml exists yet."""
        root = tmp_path / "fresh"
        root.mkdir()
        # No manifest created — set_binding should handle it
        set_binding(root, "newid", "New Project")
        assert resolve_binding(root) == "newid"
