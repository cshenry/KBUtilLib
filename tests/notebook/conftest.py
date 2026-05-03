"""Shared fixtures for notebook engine tests."""

from collections.abc import Generator
from pathlib import Path

import pytest

from kbutillib.notebook.session import NotebookSession


@pytest.fixture
def tmp_session(tmp_path: Path) -> Generator[NotebookSession, None, None]:
    """Create a NotebookSession backed by a tmpdir .kbcache/."""
    kbcache = tmp_path / ".kbcache"
    session = NotebookSession(
        kbcache_dir=kbcache,
        notebook_name="test_notebook",
        project_name="test_project",
    )
    yield session
    session.close()
