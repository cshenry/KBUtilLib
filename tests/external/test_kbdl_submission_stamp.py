"""Offline tests for stamping the submitting KOROS arc onto KBDL job submissions.

``_request`` is stubbed; the run DB lives in a tmp dir (KBDL_RUN_DB) and the
runs tree in a tmp KOROS_HOME. Assertions go through ``list_submissions``.
"""

from __future__ import annotations

import json

import pytest

from kbutillib.domains.external.kbdl_service_utils import KBDLServiceUtils
from kbutillib.koros_arc_store.store import KorosArcStore


class _Resp:
    def __init__(self, job_id):
        self._job_id = job_id

    def json(self):
        return {"job_id": self._job_id}


@pytest.fixture
def env(tmp_path, monkeypatch):
    runs = tmp_path / "home" / "runs"
    arc = runs / "projA" / "arcs" / "arc-one"
    arc.mkdir(parents=True)
    (arc / "PROVENANCE.json").write_text(json.dumps({"slug": "arc-one"}))
    db = tmp_path / "db" / "runs.sqlite"
    monkeypatch.setenv("KOROS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KBDL_RUN_DB", str(db))
    monkeypatch.setenv("KBDL_RUN_DB_ENABLED", "1")
    monkeypatch.delenv("KOROS_ARC", raising=False)
    monkeypatch.chdir(tmp_path)
    return db


def _client(monkeypatch, job_ids, **kw):
    client = KBDLServiceUtils(base_url="http://x", **kw)
    ids = iter(job_ids)
    monkeypatch.setattr(client, "_request", lambda *a, **k: _Resp(next(ids)))
    return client


def test_koros_arc_env_stamps_one_row(env, monkeypatch):
    monkeypatch.setenv("KOROS_ARC", "projA/arc-one")
    client = _client(monkeypatch, ["j1"])
    client._submit("skani", {"a": 1})
    assert KorosArcStore().list_submissions() == {"j1": ("projA", "arc-one")}


def test_no_arc_writes_nothing(env, monkeypatch):
    client = _client(monkeypatch, ["j1"])
    client._submit("skani", {})
    assert KorosArcStore().list_submissions() == {}


def test_constructor_arc_stamps_without_env(env, monkeypatch):
    client = _client(monkeypatch, ["j1"], arc="projA/arc-one")
    client._submit("skani", {})
    assert KorosArcStore().list_submissions() == {"j1": ("projA", "arc-one")}


def test_unwritable_db_still_returns_job_id(env, monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    monkeypatch.setenv("KBDL_RUN_DB", str(blocker / "sub" / "runs.sqlite"))
    monkeypatch.setenv("KOROS_ARC", "projA/arc-one")
    client = _client(monkeypatch, ["j1"])
    assert client._submit("skani", {}) == "j1"


def test_stamp_exception_is_swallowed(env, monkeypatch):
    monkeypatch.setenv("KOROS_ARC", "projA/arc-one")

    def boom(explicit=None):
        raise RuntimeError("boom")

    monkeypatch.setattr("kbutillib.arc_context.resolve_current_arc", boom)
    client = _client(monkeypatch, ["j1"])
    assert client._submit("skani", {}) == "j1"


def test_same_job_id_twice_one_row(env, monkeypatch):
    monkeypatch.setenv("KOROS_ARC", "projA/arc-one")
    client = _client(monkeypatch, ["j1", "j1"])
    client._submit("skani", {})
    client._submit("skani", {})
    assert KorosArcStore().list_submissions() == {"j1": ("projA", "arc-one")}


def test_disabled_gate_writes_nothing(env, monkeypatch):
    monkeypatch.setenv("KOROS_ARC", "projA/arc-one")
    monkeypatch.setenv("KBDL_RUN_DB_ENABLED", "0")
    client = _client(monkeypatch, ["j1"])
    client._submit("skani", {})
    monkeypatch.setenv("KBDL_RUN_DB_ENABLED", "1")
    assert KorosArcStore().list_submissions() == {}
