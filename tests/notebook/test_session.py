"""Tests for NotebookSession — for_notebook() creates .kbcache/, schema_version, idempotent."""

from pathlib import Path

import pytest

from kbutillib.notebook.session import NotebookSession
from kbutillib.notebook.storage.catalog import CURRENT_SCHEMA_VERSION


class TestSessionCreation:
    def test_creates_kbcache_dir(self, tmp_path: Path):
        kbcache = tmp_path / ".kbcache"
        assert not kbcache.exists()
        session = NotebookSession(kbcache_dir=kbcache, notebook_name="nb1")
        assert kbcache.exists()
        session.close()

    def test_notebook_name(self, tmp_path: Path):
        session = NotebookSession(
            kbcache_dir=tmp_path / ".kbcache", notebook_name="my_notebook"
        )
        assert session.notebook_name == "my_notebook"
        session.close()

    def test_schema_version(self, tmp_path: Path):
        session = NotebookSession(
            kbcache_dir=tmp_path / ".kbcache",
            notebook_name="nb",
            project_name="proj",
        )
        # Access cache to trigger catalog creation
        _ = session.cache
        row = session._get_catalog().conn.execute(
            "SELECT schema_version FROM catalog_meta"
        ).fetchone()
        assert row["schema_version"] == CURRENT_SCHEMA_VERSION
        session.close()

    def test_idempotent_creation(self, tmp_path: Path):
        """Creating session twice on same dir should not fail."""
        kbcache = tmp_path / ".kbcache"
        s1 = NotebookSession(kbcache_dir=kbcache, notebook_name="nb")
        _ = s1.cache  # Force catalog creation
        s1.close()

        s2 = NotebookSession(kbcache_dir=kbcache, notebook_name="nb")
        _ = s2.cache  # Should work fine
        # Should still have exactly one meta row
        rows = s2._get_catalog().conn.execute(
            "SELECT COUNT(*) FROM catalog_meta"
        ).fetchone()[0]
        assert rows == 1
        s2.close()


class TestSessionProperties:
    def test_cache_property(self, tmp_path: Path):
        session = NotebookSession(kbcache_dir=tmp_path / ".kbcache", notebook_name="nb")
        cache = session.cache
        assert cache is not None
        # Same instance on second access
        assert session.cache is cache
        session.close()

    def test_vectors_property(self, tmp_path: Path):
        session = NotebookSession(kbcache_dir=tmp_path / ".kbcache", notebook_name="nb")
        vs = session.vectors
        assert vs is not None
        assert session.vectors is vs
        session.close()

    def test_experiments_property(self, tmp_path: Path):
        session = NotebookSession(kbcache_dir=tmp_path / ".kbcache", notebook_name="nb")
        es = session.experiments
        assert es is not None
        assert session.experiments is es
        session.close()

    def test_strains_property(self, tmp_path: Path):
        session = NotebookSession(kbcache_dir=tmp_path / ".kbcache", notebook_name="nb")
        ss = session.strains
        assert ss is not None
        assert session.strains is ss
        session.close()


class TestForNotebook:
    def test_for_notebook_with_explicit_file(self, tmp_path: Path):
        nb_file = tmp_path / "analysis.ipynb"
        nb_file.write_text("{}")
        session = NotebookSession.for_notebook(str(nb_file))
        assert session.notebook_name == "analysis"
        assert session.kbcache_dir == tmp_path / ".kbcache"
        session.close()

    def test_for_notebook_creates_kbcache(self, tmp_path: Path):
        nb_file = tmp_path / "test.ipynb"
        nb_file.write_text("{}")
        session = NotebookSession.for_notebook(str(nb_file))
        assert session.kbcache_dir.exists()
        session.close()


class TestSessionClose:
    def test_close_is_safe(self, tmp_path: Path):
        session = NotebookSession(kbcache_dir=tmp_path / ".kbcache", notebook_name="nb")
        _ = session.cache  # Force init
        session.close()
        # Double close should not raise
        session.close()
