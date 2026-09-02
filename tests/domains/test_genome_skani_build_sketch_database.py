"""Tests for ``SKANIUtils.build_sketch_database``.

This method is a deliberate NEW addition alongside ``sketch_genome_directory``
rather than a modification of it (that method has notebook callers that
cannot be enumerated). It must avoid the three defects that disqualified
reusing/patching ``sketch_genome_directory`` for this use case:

1. hardcoding the subprocess timeout instead of honoring the caller's;
2. returning a ``database_path`` that is the PARENT of what skani actually
   wrote (skani writes to ``-o <out_dir>/sketch_db``, not ``-o <out_dir>``);
3. writing to / reading from the shared JSON database cache at all.

All tests mock ``subprocess.run`` -- no real skani binary is required.
"""
from __future__ import annotations

import inspect
import subprocess
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


def _fake_run_success(captured):
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        captured["cmd"] = cmd
        captured["timeout"] = timeout

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    return fake_run


def _fake_run_failure(captured, stderr="skani: bad input"):
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        captured["cmd"] = cmd
        captured["timeout"] = timeout

        class _Result:
            returncode = 1
            stdout = ""

        _Result.stderr = stderr
        return _Result()

    return fake_run


# ---------------------------------------------------------------------------
# database_path returned verbatim
# ---------------------------------------------------------------------------


def test_database_path_returned_verbatim(skani_utils, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run", _fake_run_success(captured))

    out_dir = str(tmp_path / "sketch_out")
    result = skani_utils.build_sketch_database(["a.fasta", "b.fasta"], out_dir)

    assert result["database_path"] == out_dir
    # Not a parent, not a child -- byte-identical to what was passed.
    assert result["database_path"] is not None
    assert result["database_path"] == out_dir


def test_database_path_verbatim_even_with_trailing_slash(skani_utils, tmp_path, monkeypatch):
    """Regression guard: no normalisation (e.g. stripping trailing slash) of out_dir."""
    captured = {}
    monkeypatch.setattr(subprocess, "run", _fake_run_success(captured))

    out_dir = str(tmp_path / "sketch_out") + "/"
    result = skani_utils.build_sketch_database(["a.fasta"], out_dir)

    assert result["database_path"] == out_dir


def test_skani_invoked_with_out_dir_directly_not_a_subpath(skani_utils, tmp_path, monkeypatch):
    """The -o argument passed to skani must be out_dir itself, not out_dir/sketch_db."""
    captured = {}
    monkeypatch.setattr(subprocess, "run", _fake_run_success(captured))

    out_dir = str(tmp_path / "sketch_out")
    skani_utils.build_sketch_database(["a.fasta"], out_dir)

    cmd = captured["cmd"]
    o_idx = cmd.index("-o") + 1
    assert cmd[o_idx] == out_dir


# ---------------------------------------------------------------------------
# caller's timeout is honored, not hardcoded 600
# ---------------------------------------------------------------------------


def test_caller_timeout_is_honored(skani_utils, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run", _fake_run_success(captured))

    skani_utils.build_sketch_database(
        ["a.fasta"], str(tmp_path / "out"), timeout=45
    )
    assert captured["timeout"] == 45


def test_default_timeout_is_600(skani_utils, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run", _fake_run_success(captured))

    skani_utils.build_sketch_database(["a.fasta"], str(tmp_path / "out"))
    assert captured["timeout"] == 600


def test_timeout_not_hardcoded_differs_from_default(skani_utils, tmp_path, monkeypatch):
    """A caller-supplied timeout that differs from 600 must actually reach subprocess.run."""
    captured = {}
    monkeypatch.setattr(subprocess, "run", _fake_run_success(captured))

    skani_utils.build_sketch_database(
        ["a.fasta"], str(tmp_path / "out"), timeout=12
    )
    assert captured["timeout"] == 12
    assert captured["timeout"] != 600


def test_timeout_expired_reports_failure_without_raising(skani_utils, tmp_path, monkeypatch):
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = skani_utils.build_sketch_database(
        ["a.fasta"], str(tmp_path / "out"), timeout=5
    )
    assert result["success"] is False
    assert result["error"] is not None


# ---------------------------------------------------------------------------
# cache file is left completely untouched
# ---------------------------------------------------------------------------


def test_cache_file_byte_identical_before_and_after_success(skani_utils, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run", _fake_run_success(captured))

    before = skani_utils.cache_file.read_bytes()

    result = skani_utils.build_sketch_database(
        ["a.fasta", "b.fasta"], str(tmp_path / "out")
    )
    assert result["success"] is True

    after = skani_utils.cache_file.read_bytes()
    assert after == before


def test_cache_file_byte_identical_before_and_after_failure(skani_utils, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run", _fake_run_failure(captured))

    before = skani_utils.cache_file.read_bytes()

    result = skani_utils.build_sketch_database(
        ["a.fasta"], str(tmp_path / "out")
    )
    assert result["success"] is False

    after = skani_utils.cache_file.read_bytes()
    assert after == before


def test_build_sketch_database_never_touches_cache_helpers(skani_utils, tmp_path, monkeypatch):
    """It must not call _load_cache, _save_cache, or _write_lock at all."""
    captured = {}
    monkeypatch.setattr(subprocess, "run", _fake_run_success(captured))

    def _forbidden(*args, **kwargs):
        raise AssertionError("build_sketch_database must not touch the cache")

    monkeypatch.setattr(skani_utils, "_load_cache", _forbidden)
    monkeypatch.setattr(skani_utils, "_save_cache", _forbidden)
    monkeypatch.setattr(skani_utils, "_write_lock", _forbidden)

    result = skani_utils.build_sketch_database(["a.fasta"], str(tmp_path / "out"))
    assert result["success"] is True


# ---------------------------------------------------------------------------
# non-zero exit -> success=False with stderr, no raise
# ---------------------------------------------------------------------------


def test_nonzero_exit_returns_failure_with_stderr(skani_utils, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        subprocess, "run", _fake_run_failure(captured, stderr="boom: invalid fasta")
    )

    result = skani_utils.build_sketch_database(["a.fasta"], str(tmp_path / "out"))

    assert result["success"] is False
    assert result["error"] == "boom: invalid fasta"
    assert result["database_path"] == str(tmp_path / "out")


def test_success_result_shape(skani_utils, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run", _fake_run_success(captured))

    out_dir = str(tmp_path / "out")
    result = skani_utils.build_sketch_database(["a.fasta", "b.fasta", "c.fasta"], out_dir)

    assert result == {
        "success": True,
        "database_path": out_dir,
        "genome_count": 3,
        "error": None,
    }


# ---------------------------------------------------------------------------
# sketch_genome_directory is unmodified
# ---------------------------------------------------------------------------


def test_sketch_genome_directory_signature_unchanged():
    sig = inspect.signature(SKANIUtils.sketch_genome_directory)
    params = list(sig.parameters)
    assert params == [
        "self",
        "fasta_directory",
        "database_name",
        "database_path",
        "description",
        "marker",
        "force_rebuild",
        "threads",
    ]
    assert sig.parameters["database_name"].default == "default"
    assert sig.parameters["database_path"].default is None
    assert sig.parameters["force_rebuild"].default is False
    assert sig.parameters["threads"].default == 1


def test_sketch_genome_directory_still_hardcodes_600s_timeout():
    """Pin the existing (unwanted-for-the-new-caller, but unchanged) behavior."""
    import kbutillib.domains.genome.skani_utils as skani_utils_module

    src = Path(skani_utils_module.__file__).read_text()
    assert "timeout=600  # 10 minute timeout" in src


def test_sketch_genome_directory_still_uses_cache_early_return(skani_utils, tmp_path):
    """Pin the existing (unwanted-for-the-new-caller, but unchanged) cache-hit shortcut."""
    skani_utils.skani_available = True
    db_dir = tmp_path / "existing"
    db_dir.mkdir()
    assert skani_utils.add_skani_database("mydb", str(db_dir), genome_count=7) is True

    fasta_dir = tmp_path / "fastas"
    fasta_dir.mkdir()
    (fasta_dir / "g1.fasta").write_text(">g1\nACGT\n")

    result = skani_utils.sketch_genome_directory(str(fasta_dir), database_name="mydb")
    assert result["success"] is True
    assert result["rebuilt"] is False
    assert result["database_path"] == str(db_dir)
    assert result["genome_count"] == 7
