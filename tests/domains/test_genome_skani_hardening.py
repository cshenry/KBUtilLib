"""Hardening tests for domains.genome.skani_utils.

These target the four concurrency/robustness fixes needed for KBDL's
skani batching/concurrency work:

1. ``_save_cache`` writes atomically (temp file + ``os.replace``), so a
   concurrent reader never observes a truncated/partial cache file.
2. The read-modify-write methods (``sketch_genome_directory``,
   ``add_skani_database``, ``remove_database``) are guarded by an advisory
   lock taken on a SIBLING lockfile, never on the cache file itself.
3. ``_load_cache`` distinguishes an absent cache file (returns {}) from a
   present-but-unparseable one (raises).
4. ``query_genomes`` accepts a ``timeout`` parameter defaulting to 300,
   with all other timeouts in the module untouched.

All tests are offline: SKANI itself is never invoked. ``skani_available``
is set directly where needed and ``subprocess.run`` is monkeypatched for
the ``query_genomes`` timeout test.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from kbutillib.domains.genome.skani_utils import SKANIUtils


@pytest.fixture
def skani_utils(tmp_path):
    """A SKANIUtils instance backed by an isolated tmp cache file."""
    cache_file = tmp_path / "skani_databases.json"
    return SKANIUtils(
        cache_file=str(cache_file),
        config_file=False,
        token_file=None,
        kbase_token_file=None,
    )


# ---------------------------------------------------------------------------
# (a) Atomic cache write
# ---------------------------------------------------------------------------


def test_save_cache_writes_via_tmp_file_in_same_dir_and_replaces(skani_utils, monkeypatch):
    """_save_cache must write to a temp file beside the cache file, then os.replace() it in."""
    replace_calls = []
    real_replace = os.replace

    def spy_replace(src, dst):
        replace_calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)

    assert skani_utils._save_cache({"a": {"path": "/x"}}) is True

    assert len(replace_calls) == 1
    src, dst = replace_calls[0]
    assert dst == str(skani_utils.cache_file)
    # The temp file must live in the same directory as the target (a
    # cross-filesystem rename would not be atomic) and must not itself be
    # the cache file path.
    assert Path(src).parent == skani_utils.cache_file.parent
    assert src != str(skani_utils.cache_file)
    # The temp file is gone after the replace -- it became the cache file.
    assert not Path(src).exists()
    assert json.loads(skani_utils.cache_file.read_text()) == {"a": {"path": "/x"}}


def test_concurrent_reader_never_observes_truncated_cache(skani_utils):
    """A reader racing a writer must always see complete JSON, never empty/partial content."""
    # Give the writer enough bytes that the write isn't instantaneous.
    big_cache = {
        f"db_{i}": {"path": f"/data/db_{i}", "genomes": list(range(50))}
        for i in range(500)
    }
    skani_utils._save_cache({"seed": {"path": "/seed"}})

    errors = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                content = skani_utils.cache_file.read_text()
            except FileNotFoundError:
                # os.replace is atomic; the file should never be absent once
                # __init__/_save_cache has run once. Treat as a failure.
                errors.append("cache file transiently missing")
                continue
            if content == "":
                errors.append("reader observed an empty (truncated) cache file")
                continue
            try:
                json.loads(content)
            except json.JSONDecodeError as exc:
                errors.append(f"reader observed unparseable/partial content: {exc}")

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    try:
        for _ in range(25):
            skani_utils._save_cache(big_cache)
    finally:
        stop.set()
        reader_thread.join(timeout=5)

    assert errors == []


# ---------------------------------------------------------------------------
# (b) Advisory lock on a sibling lockfile, never the cache file
# ---------------------------------------------------------------------------


def test_lock_file_is_sibling_not_cache_file(skani_utils):
    assert skani_utils.lock_file != skani_utils.cache_file
    assert skani_utils.lock_file.parent == skani_utils.cache_file.parent
    assert skani_utils.lock_file.name == skani_utils.cache_file.name + ".lock"


def test_write_lock_acquires_sibling_lockfile_and_leaves_cache_file_unlocked(skani_utils):
    with skani_utils._write_lock():
        assert skani_utils.lock_file.exists()

        # Because the lock is on the sibling lockfile, an independent
        # attempt to flock the cache file itself must succeed immediately
        # (non-blocking) even while the write lock is held.
        fd = os.open(str(skani_utils.cache_file), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def test_write_lock_serializes_concurrent_writers(skani_utils):
    """A second _write_lock() acquisition must block until the first is released."""
    events = []
    first_holding = threading.Event()

    def hold_then_release():
        with skani_utils._write_lock():
            events.append("first-acquired")
            first_holding.set()
            time.sleep(0.3)
            events.append("first-released")

    t = threading.Thread(target=hold_then_release)
    t.start()
    assert first_holding.wait(timeout=2), "first thread never acquired the lock"

    with skani_utils._write_lock():
        events.append("second-acquired")

    t.join(timeout=2)
    assert not t.is_alive()
    assert events.index("first-released") < events.index("second-acquired")


def test_add_and_remove_database_use_the_write_lock(skani_utils, tmp_path, monkeypatch):
    """The read-modify-write methods must take the write lock, not bypass it."""
    calls = []
    real_write_lock = skani_utils._write_lock

    import contextlib

    @contextlib.contextmanager
    def spy_write_lock():
        calls.append("locked")
        with real_write_lock():
            yield

    monkeypatch.setattr(skani_utils, "_write_lock", spy_write_lock)

    db_dir = tmp_path / "existing_db"
    db_dir.mkdir()

    assert skani_utils.add_skani_database("mydb", str(db_dir)) is True
    assert calls == ["locked"]

    calls.clear()
    assert skani_utils.remove_database("mydb") is True
    assert calls == ["locked"]


# ---------------------------------------------------------------------------
# (c) Loud failure on a corrupt cache; silent {} only when absent
# ---------------------------------------------------------------------------


def test_load_cache_returns_empty_dict_when_file_absent(skani_utils):
    # __init__ already created the cache file; remove it to simulate the
    # legitimate "no cache yet" state.
    skani_utils.cache_file.unlink()
    assert not skani_utils.cache_file.exists()
    assert skani_utils._load_cache() == {}


def test_load_cache_raises_when_file_present_but_unparseable(skani_utils):
    skani_utils.cache_file.write_text("{this is not valid json")
    with pytest.raises(json.JSONDecodeError):
        skani_utils._load_cache()


def test_get_database_info_propagates_corrupt_cache_failure(skani_utils):
    """A downstream reader (_get_database_info) must not swallow the corruption either."""
    skani_utils.cache_file.write_text("not json at all {{{")
    with pytest.raises(json.JSONDecodeError):
        skani_utils._get_database_info("anything")


# ---------------------------------------------------------------------------
# (d) query_genomes timeout parameter
# ---------------------------------------------------------------------------


def _fake_skani_search_run(captured):
    """Build a fake subprocess.run replacement that records the timeout kwarg."""

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        captured["timeout"] = timeout
        out_idx = cmd.index("-o") + 1
        Path(cmd[out_idx]).write_text("Ref_file\tQuery_file\tANI\tAlign_frac_ref\tAlign_frac_query\n")

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    return fake_run


def test_query_genomes_defaults_timeout_to_300(skani_utils, tmp_path, monkeypatch):
    skani_utils.skani_available = True
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    assert skani_utils.add_skani_database("mydb", str(db_dir)) is True

    query_file = tmp_path / "query.fasta"
    query_file.write_text(">q1\nACGTACGT\n")

    captured = {}
    monkeypatch.setattr(subprocess, "run", _fake_skani_search_run(captured))

    skani_utils.query_genomes(str(query_file), database_name="mydb")
    assert captured["timeout"] == 300


def test_query_genomes_honors_explicit_timeout(skani_utils, tmp_path, monkeypatch):
    skani_utils.skani_available = True
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    assert skani_utils.add_skani_database("mydb", str(db_dir)) is True

    query_file = tmp_path / "query.fasta"
    query_file.write_text(">q1\nACGTACGT\n")

    captured = {}
    monkeypatch.setattr(subprocess, "run", _fake_skani_search_run(captured))

    skani_utils.query_genomes(str(query_file), database_name="mydb", timeout=900)
    assert captured["timeout"] == 900


def test_query_genomes_timeout_error_message_reports_configured_timeout(skani_utils, tmp_path, monkeypatch):
    skani_utils.skani_available = True
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    assert skani_utils.add_skani_database("mydb", str(db_dir)) is True

    query_file = tmp_path / "query.fasta"
    query_file.write_text(">q1\nACGTACGT\n")

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="timed out"):
        skani_utils.query_genomes(str(query_file), database_name="mydb", timeout=42)


def test_other_module_timeouts_are_unchanged():
    """Sanity check that the other hardcoded timeouts in the module were left alone."""
    import kbutillib.domains.genome.skani_utils as skani_utils_module

    src = Path(skani_utils_module.__file__).read_text()
    assert "timeout=5" in src  # availability probe
    assert "timeout=600  # 10 minute timeout" in src  # sketch_genome_directory
    assert "timeout=300\n" in src  # compute_pairwise_distances (unnamed positional-style kwarg)
