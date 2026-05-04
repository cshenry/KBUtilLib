"""Tests for Cache — save/load, exists/info/list/delete, skip-write, access_log."""

from pathlib import Path

import pytest

from kbutillib.notebook.session import NotebookSession


class TestCacheSaveLoad:
    def test_save_and_load_dict(self, tmp_session: NotebookSession):
        cache = tmp_session.cache
        obj = {"key": "value", "nums": [1, 2, 3]}
        entry = cache.save("test_dict", obj)
        assert entry.id == "test_dict"
        assert entry.type == "dict"
        assert entry.n_bytes > 0
        assert entry.content_hash

        loaded = cache.load("test_dict")
        assert loaded == obj

    def test_save_and_load_list(self, tmp_session: NotebookSession):
        cache = tmp_session.cache
        obj = [1, 2, 3, "four"]
        cache.save("test_list", obj)
        assert cache.load("test_list") == obj

    def test_save_with_type_hint(self, tmp_session: NotebookSession):
        cache = tmp_session.cache
        cache.save("hello", "world", type_hint="text")
        result = cache.load("hello")
        assert result == "world"

    def test_save_with_metadata(self, tmp_session: NotebookSession):
        cache = tmp_session.cache
        entry = cache.save("meta_obj", {"x": 1}, metadata={"source": "test"})
        assert entry.metadata["source"] == "test"

    def test_load_missing_raises(self, tmp_session: NotebookSession):
        with pytest.raises(KeyError, match="not found"):
            tmp_session.cache.load("nonexistent")

    def test_load_with_default(self, tmp_session: NotebookSession):
        result = tmp_session.cache.load("nonexistent", default="fallback")
        assert result == "fallback"

    def test_overwrite(self, tmp_session: NotebookSession):
        cache = tmp_session.cache
        cache.save("item", {"v": 1})
        cache.save("item", {"v": 2})
        assert cache.load("item") == {"v": 2}


class TestCacheSkipWrite:
    def test_skip_write_on_unchanged_content(self, tmp_session: NotebookSession):
        cache = tmp_session.cache
        entry1 = cache.save("stable", {"x": 42})
        entry2 = cache.save("stable", {"x": 42})
        # Same content hash — second write should be a no-op for the blob
        assert entry1.content_hash == entry2.content_hash


class TestCacheExists:
    def test_exists_true(self, tmp_session: NotebookSession):
        tmp_session.cache.save("present", [1])
        assert tmp_session.cache.exists("present") is True

    def test_exists_false(self, tmp_session: NotebookSession):
        assert tmp_session.cache.exists("absent") is False


class TestCacheInfo:
    def test_info(self, tmp_session: NotebookSession):
        tmp_session.cache.save("info_test", {"a": 1})
        entry = tmp_session.cache.info("info_test")
        assert entry.id == "info_test"
        assert entry.type == "dict"
        assert entry.content_hash
        assert entry.n_bytes > 0

    def test_info_missing_raises(self, tmp_session: NotebookSession):
        with pytest.raises(KeyError):
            tmp_session.cache.info("nope")


class TestCacheList:
    def test_list_all(self, tmp_session: NotebookSession):
        cache = tmp_session.cache
        cache.save("a", [1])
        cache.save("b", {"x": 2})
        entries = cache.list()
        ids = [e.id for e in entries]
        assert "a" in ids
        assert "b" in ids

    def test_list_type_filter(self, tmp_session: NotebookSession):
        cache = tmp_session.cache
        cache.save("d1", {"x": 1})
        cache.save("l1", [1, 2])
        entries = cache.list(type_filter="dict")
        assert all(e.type == "dict" for e in entries)


class TestCacheDelete:
    def test_delete(self, tmp_session: NotebookSession):
        cache = tmp_session.cache
        cache.save("to_delete", {"x": 1})
        assert cache.exists("to_delete")
        cache.delete("to_delete")
        assert not cache.exists("to_delete")

    def test_delete_missing_raises(self, tmp_session: NotebookSession):
        with pytest.raises(KeyError):
            tmp_session.cache.delete("nope")

    def test_delete_removes_blob_file(self, tmp_session: NotebookSession):
        cache = tmp_session.cache
        entry = cache.save("blob_test", {"data": "content"})
        blob_path = tmp_session.kbcache_dir / "blobs" / f"{entry.content_hash}.json"
        assert blob_path.exists()
        cache.delete("blob_test")
        assert not blob_path.exists()

    def test_delete_preserves_shared_blob(self, tmp_session: NotebookSession):
        """If two entries share a hash, delete one should not remove the blob."""
        cache = tmp_session.cache
        obj = {"shared": True}
        entry1 = cache.save("copy1", obj)
        entry2 = cache.save("copy2", obj)
        assert entry1.content_hash == entry2.content_hash

        cache.delete("copy1")
        # Blob still exists because copy2 references it
        blob_path = tmp_session.kbcache_dir / "blobs" / f"{entry2.content_hash}.json"
        assert blob_path.exists()
        # copy2 still loads
        assert cache.load("copy2") == obj


class TestCrossNotebookCache:
    """Cache is project-wide: save in notebook A, load in notebook B."""

    def test_cross_notebook_load(self, tmp_path):
        """Object saved by notebook A is visible to notebook B."""
        from kbutillib.notebook.session import NotebookSession

        kbcache = tmp_path / ".kbcache"
        sess_a = NotebookSession(kbcache_dir=kbcache, notebook_name="notebook_a")
        sess_a.cache.save("shared_obj", {"cross": True})
        sess_a.close()

        sess_b = NotebookSession(kbcache_dir=kbcache, notebook_name="notebook_b")
        loaded = sess_b.cache.load("shared_obj")
        assert loaded == {"cross": True}
        sess_b.close()

    def test_list_created_by_notebook(self, tmp_path):
        """list(created_by_notebook=) filters by originating notebook."""
        from kbutillib.notebook.session import NotebookSession

        kbcache = tmp_path / ".kbcache"
        sess_a = NotebookSession(kbcache_dir=kbcache, notebook_name="nb_alpha")
        sess_a.cache.save("alpha_obj", [1])
        sess_a.close()

        sess_b = NotebookSession(kbcache_dir=kbcache, notebook_name="nb_beta")
        sess_b.cache.save("beta_obj", [2])

        # Without filter: both visible
        all_entries = sess_b.cache.list()
        all_ids = [e.id for e in all_entries]
        assert "alpha_obj" in all_ids
        assert "beta_obj" in all_ids

        # With filter: only the target notebook's objects
        alpha_entries = sess_b.cache.list(created_by_notebook="nb_alpha")
        alpha_ids = [e.id for e in alpha_entries]
        assert "alpha_obj" in alpha_ids
        assert "beta_obj" not in alpha_ids

        beta_entries = sess_b.cache.list(created_by_notebook="nb_beta")
        beta_ids = [e.id for e in beta_entries]
        assert "beta_obj" in beta_ids
        assert "alpha_obj" not in beta_ids
        sess_b.close()

    def test_list_created_by_notebook_empty(self, tmp_session: NotebookSession):
        """Filtering by a non-existent notebook returns empty list."""
        tmp_session.cache.save("something", {"x": 1})
        entries = tmp_session.cache.list(created_by_notebook="nonexistent_nb")
        assert entries == []


class TestCacheAccessLog:
    def test_write_logged(self, tmp_session: NotebookSession):
        cache = tmp_session.cache
        cache.save("logged", [1])
        rows = tmp_session._get_catalog().conn.execute(
            "SELECT * FROM access_log WHERE object_id='logged' AND op='write'"
        ).fetchall()
        assert len(rows) == 1

    def test_read_logged(self, tmp_session: NotebookSession):
        cache = tmp_session.cache
        cache.save("r", [1])
        cache.load("r")
        rows = tmp_session._get_catalog().conn.execute(
            "SELECT * FROM access_log WHERE object_id='r' AND op='read'"
        ).fetchall()
        assert len(rows) == 1

    def test_delete_logged(self, tmp_session: NotebookSession):
        cache = tmp_session.cache
        cache.save("d", [1])
        cache.delete("d")
        rows = tmp_session._get_catalog().conn.execute(
            "SELECT * FROM access_log WHERE object_id='d' AND op='delete'"
        ).fetchall()
        assert len(rows) == 1
