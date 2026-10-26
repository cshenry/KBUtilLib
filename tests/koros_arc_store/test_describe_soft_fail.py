"""describe_record / set_scope_description soft-fail, like every other write.

They once incremented ``self._failed_writes`` and called ``log.warning``, names
that do not exist in run_db.py, so the failure path itself raised.
"""

from __future__ import annotations

import pytest

from kbutillib.koros_arc_store.run_db import RunDatabase


@pytest.fixture
def db(tmp_path):
    return RunDatabase(db_path=tmp_path / "runs.sqlite", enabled=True)


def _boom(*args, **kwargs):
    raise RuntimeError("boom")


def test_describe_record_soft_fails_when_connect_raises(db, monkeypatch):
    monkeypatch.setattr(db, "_connect", _boom)
    assert db.describe_record("rec", description="x") is False
    assert db.failed_write_count == 1


def test_describe_record_soft_fails_on_body_error(db, monkeypatch, caplog):
    monkeypatch.setattr(db, "_ensure_initialized_for_write", _boom)
    assert db.describe_record("rec", description="x") is False
    assert db.failed_write_count == 1
    assert "describe_record failed" in caplog.text


def test_set_scope_description_soft_fails_when_connect_raises(db, monkeypatch):
    monkeypatch.setattr(db, "_connect", _boom)
    assert db.set_scope_description("proj", None, "d") is False
    assert db.failed_write_count == 1


def test_set_scope_description_soft_fails_on_body_error(db, monkeypatch, caplog):
    monkeypatch.setattr(db, "_ensure_initialized_for_write", _boom)
    assert db.set_scope_description("proj", "arc", "d") is False
    assert db.failed_write_count == 1
    assert "set_scope_description failed" in caplog.text
