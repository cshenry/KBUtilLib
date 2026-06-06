"""Shared fixtures for kbu CLI tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from kbutillib.cli.manifest import (
    now_utc_iso,
    write_project_manifest,
    write_subproject_manifest,
)


@pytest.fixture()
def tmp_kbu_project(tmp_path: Path) -> Path:
    """Create a temporary directory with a seeded ``kbu-project.toml``.

    Returns the project root path.
    """
    root = tmp_path / "my_project"
    root.mkdir()

    now = now_utc_iso()
    project_data = {
        "project": {
            "name": "my_project",
            "title": "My test project",
            "created_at": now,
            "authors": [
                {
                    "name": "Test User",
                    "affiliation": "Test Lab",
                    "orcid": "0000-0000-0000-0000",
                }
            ],
        },
        "kbutillib": {
            "source_path": "/fake/kbutillib",
            "source_commit": "abc123",
        },
        "update": {
            "last_pulled_at": now,
            "last_pulled_commit": "abc123",
        },
    }
    write_project_manifest(root, project_data)
    return root


@pytest.fixture()
def tmp_subproject(tmp_kbu_project: Path) -> tuple[Path, str]:
    """Create a subproject named ``test_sp`` inside *tmp_kbu_project*.

    Returns ``(project_root, subproject_name)``.
    """
    root = tmp_kbu_project
    name = "test_sp"
    now = now_utc_iso()

    sp_dir = root / "subprojects" / name
    sp_dir.mkdir(parents=True)
    (sp_dir / "notebooks").mkdir()
    (sp_dir / "sessions").mkdir()

    sp_data = {
        "subproject": {
            "name": name,
            "title": "Test subproject",
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
    write_subproject_manifest(root, name, sp_data)
    return root, name
