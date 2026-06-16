"""Tests for NotebookSession.for_notebook() cache_dir parameter (Module 3).

AC 15: for_notebook() accepts cache_dir; omitted -> .kbcache (BERIL default
       unchanged); cache_dir='NBCache' writes/reads cache under NBCache/.
AC 16: A cached object written through a NotebookSession with cache_dir='NBCache'
       round-trips (write then read) from that directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kbutillib.notebook.session import NotebookSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_notebook(base: Path, name: str = "analysis.ipynb") -> Path:
    """Write a minimal notebook stub and return its path."""
    nb = base / name
    nb.write_text("{}")
    return nb


# ---------------------------------------------------------------------------
# AC 15a — omitted cache_dir defaults to .kbcache (BERIL back-compat)
# ---------------------------------------------------------------------------


class TestDefaultCacheDir:
    def test_default_is_kbcache(self, tmp_path: Path):
        """for_notebook() with no cache_dir produces .kbcache alongside the notebook."""
        nb = _make_notebook(tmp_path)
        session = NotebookSession.for_notebook(str(nb))
        try:
            assert session.kbcache_dir == tmp_path / ".kbcache"
        finally:
            session.close()

    def test_default_creates_kbcache_dir(self, tmp_path: Path):
        nb = _make_notebook(tmp_path)
        session = NotebookSession.for_notebook(str(nb))
        try:
            assert session.kbcache_dir.exists()
            assert session.kbcache_dir.name == ".kbcache"
        finally:
            session.close()

    def test_explicit_none_is_same_as_omitted(self, tmp_path: Path):
        """Passing cache_dir=None explicitly is identical to omitting it."""
        nb = _make_notebook(tmp_path)
        session = NotebookSession.for_notebook(str(nb), cache_dir=None)
        try:
            assert session.kbcache_dir == tmp_path / ".kbcache"
        finally:
            session.close()


# ---------------------------------------------------------------------------
# AC 15b — cache_dir='NBCache' places cache in NBCache/
# ---------------------------------------------------------------------------


class TestCustomCacheDir:
    def test_nbcache_dir_name(self, tmp_path: Path):
        """cache_dir='NBCache' places the cache directory at <parent>/NBCache/."""
        nb = _make_notebook(tmp_path)
        session = NotebookSession.for_notebook(str(nb), cache_dir="NBCache")
        try:
            assert session.kbcache_dir == tmp_path / "NBCache"
        finally:
            session.close()

    def test_nbcache_dir_created(self, tmp_path: Path):
        nb = _make_notebook(tmp_path)
        session = NotebookSession.for_notebook(str(nb), cache_dir="NBCache")
        try:
            assert session.kbcache_dir.exists()
        finally:
            session.close()

    def test_no_kbcache_dir_created_when_nbcache_used(self, tmp_path: Path):
        """When cache_dir='NBCache', the default .kbcache/ must NOT be created."""
        nb = _make_notebook(tmp_path)
        session = NotebookSession.for_notebook(str(nb), cache_dir="NBCache")
        try:
            assert not (tmp_path / ".kbcache").exists()
        finally:
            session.close()

    def test_arbitrary_cache_dir_name(self, tmp_path: Path):
        """Any non-None cache_dir string is used as the directory name."""
        nb = _make_notebook(tmp_path)
        session = NotebookSession.for_notebook(str(nb), cache_dir="MyCache")
        try:
            assert session.kbcache_dir == tmp_path / "MyCache"
            assert session.kbcache_dir.exists()
        finally:
            session.close()


# ---------------------------------------------------------------------------
# AC 16 — round-trip write then read from NBCache/
# ---------------------------------------------------------------------------


class TestCacheDirRoundTrip:
    def test_round_trip_nbcache(self, tmp_path: Path):
        """An object saved with cache_dir='NBCache' is read back from NBCache/."""
        nb = _make_notebook(tmp_path)

        # Write
        session_w = NotebookSession.for_notebook(
            str(nb), project_name="test-proj", cache_dir="NBCache"
        )
        session_w.cache.save("result", {"value": 42, "label": "hello"})
        session_w.close()

        # Confirm NBCache/ exists and .kbcache/ was never created
        assert (tmp_path / "NBCache").exists()
        assert not (tmp_path / ".kbcache").exists()

        # Read — open a fresh session on the same cache_dir
        session_r = NotebookSession.for_notebook(
            str(nb), project_name="test-proj", cache_dir="NBCache"
        )
        try:
            loaded = session_r.cache.load("result")
            assert loaded == {"value": 42, "label": "hello"}
        finally:
            session_r.close()

    def test_round_trip_default_still_uses_kbcache(self, tmp_path: Path):
        """Round-trip through the default (.kbcache) path still works unchanged."""
        nb = _make_notebook(tmp_path)

        session_w = NotebookSession.for_notebook(str(nb), project_name="test-proj")
        session_w.cache.save("data", [1, 2, 3])
        session_w.close()

        assert (tmp_path / ".kbcache").exists()

        session_r = NotebookSession.for_notebook(str(nb), project_name="test-proj")
        try:
            loaded = session_r.cache.load("data")
            assert loaded == [1, 2, 3]
        finally:
            session_r.close()

    def test_nbcache_and_kbcache_are_independent(self, tmp_path: Path):
        """Data written to NBCache/ is not visible from .kbcache/ and vice versa."""
        nb = _make_notebook(tmp_path)

        # Write to NBCache
        s1 = NotebookSession.for_notebook(str(nb), cache_dir="NBCache")
        s1.cache.save("shared_key", "from-nbcache")
        s1.close()

        # Write to .kbcache (default)
        s2 = NotebookSession.for_notebook(str(nb))
        s2.cache.save("shared_key", "from-kbcache")
        s2.close()

        # Read back from NBCache — should see NBCache value
        s3 = NotebookSession.for_notebook(str(nb), cache_dir="NBCache")
        try:
            assert s3.cache.load("shared_key") == "from-nbcache"
        finally:
            s3.close()

        # Read back from .kbcache — should see kbcache value
        s4 = NotebookSession.for_notebook(str(nb))
        try:
            assert s4.cache.load("shared_key") == "from-kbcache"
        finally:
            s4.close()
