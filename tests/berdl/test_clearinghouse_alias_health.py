"""Tests for ``alias_orphans`` and its place in ``kbu clearinghouse health``.

The load-bearing property: an alias orphan is LEGAL (the identifier arrives
before the entity), so a non-zero orphan count must leave ``health`` exiting
zero, the report must carry no ``'failures'`` key, and the count must not be a
warning -- while a parameter-set finding must still exit non-zero.
"""

from __future__ import annotations

import json
import re

import pytest
from click.testing import CliRunner

import kbutillib.interfaces.cli.clearinghouse as ch
from kbutillib.cli import main
from kbutillib.domains.kbase.berdl.clearinghouse_capability import (
    ClearinghouseCapability,
)
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    ENTITY_TYPES,
    PARAMETER_SET_TABLE,
    table_configs,
    table_name,
)

from .test_clearinghouse_capability import _FakeWriteCapability

HA = "aa" * 32
HB = "bb" * 32


def arow(h, obs, t, *, src="kbdl_object", aid="obj1", etype="genome", missing=True,
         batch="b1"):
    return {
        "entity_type": etype,
        "entity_hash": h,
        "alias_source": src,
        "alias_id": aid,
        "observation": obs,
        "observed_at": t,
        "ingest_batch_id": batch,
        "entity_missing": missing,
    }


class _AliasFake(_FakeWriteCapability):
    """Answers each ``<type>_alias`` read with that type's stored rows."""

    def __init__(self, rows, **kw):
        super().__init__(**kw)
        self.rows = rows

    def query(self, sql, *, params=None, engine=None, **kwargs):
        self.queries.append(sql)
        m = re.search(r"(\w+)_alias\"\s+AS a", sql)
        assert m, sql
        return [dict(r) for r in self.rows if r["entity_type"] == m.group(1)]


def _report(rows):
    return ClearinghouseCapability(capability=_AliasFake(rows)).alias_orphans()


def test_report_groups_by_type_and_source_and_has_no_failures_key():
    rep = _report(
        [
            arow(HA, "present", 1, aid="a1"),
            arow(HA, "present", 1, aid="a2"),
            arow(HB, "present", 1, src="other", aid="a3"),
            arow(HA, "present", 1, etype="protein", aid="p1"),
            arow(HB, "present", 1, aid="ok", missing=False),
        ]
    )
    assert rep["orphan_total"] == 4
    assert rep["orphans_by_source"] == [
        {"entity_type": "genome", "alias_source": "kbdl_object", "orphan_count": 2},
        {"entity_type": "genome", "alias_source": "other", "orphan_count": 1},
        {"entity_type": "protein", "alias_source": "kbdl_object", "orphan_count": 1},
    ]
    assert rep["reduced_rows"] == 5
    assert "failures" not in rep


def test_each_type_joins_its_own_entity_table():
    fake = _AliasFake([])
    ClearinghouseCapability(capability=fake).alias_orphans()
    assert len(fake.queries) == len(ENTITY_TYPES) == 5
    for etype, sql in zip(ENTITY_TYPES, fake.queries):
        assert f'"{table_name(etype, "alias")}"' in sql
        assert f'"{table_name(etype, "entity")}"' in sql
        for other in ENTITY_TYPES:
            if other != etype:
                assert f'"{table_name(other, "entity")}"' not in sql


def test_withdrawn_orphan_is_not_counted_but_re_present_is():
    withdrawn = [arow(HA, "present", 1), arow(HA, "withdrawn", 2)]
    rep = _report(withdrawn)
    assert rep["orphan_total"] == 0
    assert rep["reduced_rows"] == 1  # the population checked is still visible
    assert _report(withdrawn + [arow(HA, "present", 3)])["orphan_total"] == 1


# -- CLI ---------------------------------------------------------------------

_BROKEN_REGISTRY = {
    "ok": False,
    "failures": ["parameter_set_hash cc.. has no registry row"],
}


class _HealthFake:
    def __init__(self, alias_report, ps_report=None):
        self._alias = alias_report
        self._ps = ps_report or {"ok": True, "failures": []}

    def _locus(self):
        return "in_pod"

    def stats(self, **_kw):
        return {"tables": [], "warnings": []}

    def parameter_set_orphans(self, **_kw):
        return dict(self._ps)

    def alias_orphans(self, **_kw):
        return dict(self._alias)


def _health(monkeypatch, fake):
    monkeypatch.setattr(ch, "_capability", lambda: fake)
    result = CliRunner().invoke(
        main, ["clearinghouse", "health", "--json"], catch_exceptions=False
    )
    return result, json.loads(result.output.splitlines()[-1])


def _real_alias_report(rows):
    return _report(rows)


def test_nonzero_alias_orphans_exit_zero_and_are_not_a_warning(monkeypatch):
    report = _real_alias_report([arow(HA, "present", 1)])
    assert report["orphan_total"] == 1
    assert "failures" not in report
    result, payload = _health(monkeypatch, _HealthFake(report))
    assert result.exit_code == 0
    assert payload["data"]["alias_integrity"]["orphan_total"] == 1
    assert "failures" not in payload["data"]["alias_integrity"]
    assert not any(
        "alias" in w.lower() or "orphan" in w.lower() for w in payload["warnings"]
    )


def test_parameter_set_finding_still_exits_nonzero_with_alias_orphans(monkeypatch):
    report = _real_alias_report([arow(HA, "present", 1)])
    result, payload = _health(monkeypatch, _HealthFake(report, _BROKEN_REGISTRY))
    assert result.exit_code == 1
    assert payload["data"]["alias_integrity"]["orphan_total"] == 1
    assert payload["data"]["parameter_set_integrity"]["failures"]


def test_health_keeps_pre_existing_keys_and_adds_alias_integrity(monkeypatch):
    _, payload = _health(
        monkeypatch, _HealthFake({"orphans_by_source": [], "orphan_total": 0})
    )
    assert {
        "threshold_bytes",
        "tables_evaluated",
        "tables",
        "parameter_set_integrity",
        "alias_integrity",
    } <= set(payload["data"])


# -- regression guard: the lake-5 preflight must not learn about alias tables --


class _OldNamespace(_FakeWriteCapability):
    """A namespace holding exactly the sixteen pre-existing tables."""

    def __init__(self):
        super().__init__()
        self.tables = {}
        for cfg in table_configs():
            if cfg["name"].endswith("_alias"):
                continue
            cols = ["entity_hash"]
            if cfg["name"].endswith("_result"):
                cols.append("parameter_set_hash")
            self.tables[cfg["name"]] = cols

    def query(self, sql, *, params=None, engine=None, **kwargs):
        m = re.search(r"DESCRIBE\s+\S*?`?(\w+)`?\s*$", sql.strip())
        assert m, sql
        name = m.group(1)
        if name not in self.tables:
            raise RuntimeError("table not found")
        return [{"col_name": c} for c in self.tables[name]]


def test_lake5_preflight_passes_without_any_alias_table():
    fake = _OldNamespace()
    assert len(fake.tables) == 16
    assert not any(n.endswith("_alias") for n in fake.tables)
    assert PARAMETER_SET_TABLE in fake.tables
    ClearinghouseCapability(capability=fake)._assert_lake5_schema("tenant.ns")
