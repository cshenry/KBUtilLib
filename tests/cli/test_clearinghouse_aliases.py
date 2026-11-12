"""Tests for ``kbu clearinghouse aliases`` and ``resolve``.

Driven against a fake capability injected via ``_capability``; no network.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

import kbutillib.interfaces.cli.clearinghouse as ch
from kbutillib.cli import main
from kbutillib.domains.kbase.berdl.clearinghouse_capability import (
    AliasAmbiguousError,
    AliasNotInvertibleError,
)
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    ALIAS_SOURCES,
    INVERTIBLE_ALIAS_SOURCES,
)

_KEYS = {"schema_version", "verb", "generated_at", "locus", "data", "warnings"}
_H = "a" * 64


class _Fake:
    def __init__(self, resolve_result=_H, resolve_exc=None):
        self.calls = {}
        self._resolve_result = resolve_result
        self._resolve_exc = resolve_exc

    def _locus(self):
        return "in_pod"

    def aliases(self, entity_type=None, entity_hash=None, *, alias_sources=None,
                alias_ids=None, **_kw):
        self.calls["aliases"] = {
            "entity_type": entity_type,
            "entity_hash": entity_hash,
            "alias_sources": alias_sources,
            "alias_ids": alias_ids,
        }
        return [
            {"entity_type": "genome", "entity_hash": _H, "alias_source": "kbdl_object",
             "alias_id": "obj1", "observation": "present"}
        ]

    def resolve(self, alias_source, alias_id, *, entity_type=None, **_kw):
        self.calls["resolve"] = {
            "alias_source": alias_source,
            "alias_id": alias_id,
            "entity_type": entity_type,
        }
        if alias_source not in ALIAS_SOURCES:
            raise ValueError(f"Unknown alias_source {alias_source!r}")
        if self._resolve_exc is not None:
            raise self._resolve_exc
        return self._resolve_result


def _run(monkeypatch, fake, argv):
    monkeypatch.setattr(ch, "_capability", lambda: fake)
    return CliRunner().invoke(main, ["clearinghouse", *argv])


def test_schema_version_still_one():
    assert ch.SCHEMA_VERSION == 1


def test_aliases_json_and_human(monkeypatch):
    fake = _Fake()
    res = _run(monkeypatch, fake, ["aliases", "--json"])
    assert res.exit_code == 0
    env = json.loads(res.output)
    assert set(env) == _KEYS and env["verb"] == "aliases"
    assert env["schema_version"] == 1
    assert env["data"]["aliases"][0]["alias_id"] == "obj1"
    res = _run(monkeypatch, fake, ["aliases"])
    assert res.exit_code == 0 and "obj1" in res.output


def test_aliases_unpassed_filters_are_none_not_empty(monkeypatch):
    fake = _Fake()
    _run(monkeypatch, fake, ["aliases"])
    assert fake.calls["aliases"]["alias_sources"] is None
    assert fake.calls["aliases"]["alias_ids"] is None
    assert fake.calls["aliases"]["entity_type"] is None


def test_aliases_repeated_values_arrive_as_list(monkeypatch):
    fake = _Fake()
    _run(monkeypatch, fake, [
        "aliases", "--type", "genome", "--hash", _H,
        "--alias-source", "kbdl_object", "--alias-source", "ncbi_assembly",
        "--alias-id", "x", "--alias-id", "y",
    ])
    assert fake.calls["aliases"] == {
        "entity_type": "genome",
        "entity_hash": _H,
        "alias_sources": ["kbdl_object", "ncbi_assembly"],
        "alias_ids": ["x", "y"],
    }


def test_resolve_found_json_and_human(monkeypatch):
    fake = _Fake()
    res = _run(monkeypatch, fake, ["resolve", "--alias-source", "kbdl_object",
                                   "--alias-id", "obj1", "--json"])
    assert res.exit_code == 0
    env = json.loads(res.output)
    assert set(env) == _KEYS and env["verb"] == "resolve"
    assert env["data"]["found"] is True and env["data"]["hash"] == _H
    assert fake.calls["resolve"]["entity_type"] is None
    res = _run(monkeypatch, fake, ["resolve", "--alias-source", "kbdl_object",
                                   "--alias-id", "obj1", "--type", "genome"])
    assert res.exit_code == 0 and res.output.strip() == _H
    assert fake.calls["resolve"]["entity_type"] == "genome"


def test_resolve_unknown_alias_exits_zero_and_is_readable(monkeypatch):
    fake = _Fake(resolve_result=None)
    res = _run(monkeypatch, fake, ["resolve", "--alias-source", "kbdl_object",
                                   "--alias-id", "nope", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.output)["data"]
    assert data["found"] is False and data["hash"] is None
    res = _run(monkeypatch, fake, ["resolve", "--alias-source", "kbdl_object",
                                   "--alias-id", "nope"])
    assert res.exit_code == 0 and "not known" in res.output


def _assert_one_failure(res, kind):
    assert res.exit_code == 1
    assert isinstance(res.exception, SystemExit)  # not a escaped exception
    env = json.loads(res.stdout)  # exactly one object, nothing else
    assert env["data"]["error"]["kind"] == kind
    assert env["data"]["found"] is False
    assert "Traceback" not in res.output
    return env


def test_resolve_ambiguous_exits_one_with_one_envelope(monkeypatch):
    fake = _Fake(resolve_exc=AliasAmbiguousError("contested"))
    res = _run(monkeypatch, fake, ["resolve", "--alias-source", "kbdl_object",
                                   "--alias-id", "x", "--json"])
    _assert_one_failure(res, "ambiguous")


def test_resolve_invalid_source_exits_one(monkeypatch):
    res = _run(monkeypatch, _Fake(), ["resolve", "--alias-source", "bogus",
                                      "--alias-id", "x", "--json"])
    _assert_one_failure(res, "invalid_input")


@pytest.mark.parametrize("source", ["ncbi_taxon", "arc_analysis"])
def test_resolve_non_invertible_exits_one_naming_aliases(monkeypatch, source):
    assert source not in INVERTIBLE_ALIAS_SOURCES
    fake = _Fake(resolve_exc=AliasNotInvertibleError(f"{source} not invertible"))
    res = _run(monkeypatch, fake, ["resolve", "--alias-source", source,
                                   "--alias-id", "562", "--json"])
    env = _assert_one_failure(res, "not_invertible")
    assert "aliases" in env["data"]["error"]["message"]


def test_resolve_failure_human_mode_no_traceback(monkeypatch):
    fake = _Fake(resolve_exc=AliasAmbiguousError("contested"))
    res = _run(monkeypatch, fake, ["resolve", "--alias-source", "kbdl_object",
                                   "--alias-id", "x"])
    assert res.exit_code == 1 and "contested" in res.output
    assert "Traceback" not in res.output


def test_resolve_transport_error_is_non_zero(monkeypatch):
    fake = _Fake(resolve_exc=ConnectionError("down"))
    res = _run(monkeypatch, fake, ["resolve", "--alias-source", "kbdl_object",
                                   "--alias-id", "x"])
    assert res.exit_code != 0
