"""Tests for ``aliases`` / ``resolve`` (the alias read surface).

The stub evaluates the filters it can see in the EMITTED SQL (``1 = 0``,
``alias_source IN (...)``, ``alias_id IN (...)``, the ``entity_hash`` bind) against
an in-memory row store, and returns the store's rows UNREDUCED, so the
capability's own reduction -- not the stub -- is what the tests exercise.
"""

from __future__ import annotations

import re

import pytest

from kbutillib.domains.kbase.berdl.clearinghouse_capability import (
    AliasAmbiguousError,
    AliasNotInvertibleError,
    ClearinghouseCapability,
)
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    ALIAS_SOURCES,
    INVERTIBLE_ALIAS_SOURCES,
    table_name,
)

from .test_clearinghouse_capability import _FakeWriteCapability

HA = "aa" * 32
HB = "bb" * 32
SRC = "kbdl_object"


def row(h, obs, t, *, src=SRC, aid="obj1", etype="genome", batch="b1"):
    return {
        "entity_type": etype,
        "entity_hash": h,
        "alias_source": src,
        "alias_id": aid,
        "observation": obs,
        "observed_at": t,
        "ingest_batch_id": batch,
    }


class _ReadFake(_FakeWriteCapability):
    def __init__(self, rows, **kw):
        super().__init__(**kw)
        self.rows = rows
        self.sql: list[str] = []
        self.params: list = []

    def query(self, sql, *, params=None, engine=None, **kwargs):
        self.sql.append(sql)
        self.params.append(params)
        out = list(self.rows)
        if "1 = 0" in sql:
            return []
        for col in ("alias_source", "alias_id"):
            m = re.search(rf"{col} IN \(([^)]*)\)", sql)
            if m:
                allowed = set(re.findall(r"'([^']*)'", m.group(1)))
                out = [r for r in out if r[col] in allowed]
        if "entity_hash = ?" in sql:
            out = [r for r in out if r["entity_hash"] == params[0]]
        return [dict(r) for r in out]


def _cap(rows):
    fake = _ReadFake(rows)
    return ClearinghouseCapability(capability=fake), fake


# -- aliases: filters ---------------------------------------------------------


def test_none_applies_no_filter_and_empty_list_matches_nothing():
    rows = [row(HA, "present", 1), row(HB, "present", 1, aid="obj2")]
    cap, fake = _cap(rows)
    assert len(cap.aliases("genome", alias_sources=None, alias_ids=None)) == 2
    assert "alias_source IN" not in fake.sql[-1]
    assert cap.aliases("genome", alias_sources=[]) == []
    assert "1 = 0" in fake.sql[-1] and "IN ()" not in fake.sql[-1]
    assert cap.aliases("genome", alias_ids=[]) == []
    assert "1 = 0" in fake.sql[-1] and "IN ()" not in fake.sql[-1]


def test_filters_compose_by_and():
    rows = [
        row(HA, "present", 1, aid="x"),
        row(HA, "present", 1, aid="y"),
        row(HB, "present", 1, aid="x"),
        row(HA, "present", 1, src="kbdl_fasta_object", aid="x"),
    ]
    cap, _ = _cap(rows)
    got = cap.aliases(
        "genome", HA, alias_sources=[SRC], alias_ids=["x"]
    )
    assert [(r["entity_hash"], r["alias_id"], r["alias_source"]) for r in got] == [
        (HA, "x", SRC)
    ]


def test_entity_hash_is_a_placeholder_with_canonical_param():
    cap, fake = _cap([row(HA, "present", 1)])
    cap.aliases("genome", HA.upper())
    assert "entity_hash = ?" in fake.sql[-1]
    assert fake.params[-1] == [HA]
    with pytest.raises(ValueError):
        cap.aliases("genome", "nothex")


def test_duplicates_collapse_to_one_row_per_five_tuple():
    r = row(HA, "present", 5)
    cap, _ = _cap([r, dict(r), dict(r, ingest_batch_id="b0", observed_at=3)])
    got = cap.aliases("genome")
    assert len(got) == 1 and got[0]["observed_at"] == 5


def test_history_is_latest_occurrence_of_each_state():
    rows = [
        row(HA, "present", 1),
        row(HA, "withdrawn", 2),
        row(HA, "present", 3),
    ]
    cap, _ = _cap(rows)
    got = cap.aliases("genome")
    assert sorted((r["observation"], r["observed_at"]) for r in got) == [
        ("present", 3),
        ("withdrawn", 2),
    ]


def test_current_only_drops_withdrawn_winners():
    cap, _ = _cap([row(HA, "present", 1), row(HA, "withdrawn", 2), row(HB, "present", 1)])
    got = cap.aliases("genome", current_only=True)
    assert [r["entity_hash"] for r in got] == [HB]


def test_current_only_returns_latest_present_row():
    cap, _ = _cap([row(HA, "present", 1), row(HA, "withdrawn", 2), row(HA, "present", 3)])
    got = cap.aliases("genome", current_only=True)
    assert len(got) == 1 and got[0]["observed_at"] == 3


def test_current_only_finally_withdrawn_is_empty():
    cap, _ = _cap([row(HA, "present", 1), row(HA, "withdrawn", 2)])
    assert cap.aliases("genome", current_only=True) == []


def test_exact_tie_resolves_to_withdrawn():
    cap, _ = _cap([row(HA, "present", 1), row(HA, "withdrawn", 1)])
    assert cap.aliases("genome", current_only=True) == []


def test_entity_type_selects_table_or_all_alias_view():
    cap, fake = _cap([])
    cap.aliases("protein")
    assert table_name("protein", "alias") in fake.sql[-1]
    assert "all_alias" not in fake.sql[-1]
    cap.aliases(None)
    assert "all_alias" in fake.sql[-1]
    cap.resolve(SRC, "o", entity_type="gene")
    assert table_name("gene", "alias") in fake.sql[-1]
    cap.resolve(SRC, "o")
    assert "all_alias" in fake.sql[-1]


# -- resolve ------------------------------------------------------------------


def test_resolve_unknown_alias_none_unknown_source_valueerror():
    cap, _ = _cap([row(HA, "present", 1)])
    assert cap.resolve(SRC, "nope") is None
    with pytest.raises(ValueError):
        cap.resolve("not_a_source", "obj1")
    with pytest.raises(ValueError) as ei:
        cap.resolve("not_a_source", "obj1")
    assert not isinstance(ei.value, AliasAmbiguousError)


def test_ambiguous_error_is_distinct_and_names_both_hashes():
    assert issubclass(AliasAmbiguousError, ValueError) is False
    assert issubclass(AliasAmbiguousError, AliasNotInvertibleError) is False
    cap, _ = _cap([row(HA, "present", 1), row(HB, "present", 2)])
    with pytest.raises(AliasAmbiguousError) as ei:
        cap.resolve(SRC, "obj1")
    assert type(ei.value) is AliasAmbiguousError
    assert HA in str(ei.value) and HB in str(ei.value)
    assert SRC in str(ei.value) and "obj1" in str(ei.value)


def test_resolve_tracks_latest_observation():
    cap, _ = _cap([row(HA, "present", 1)])
    assert cap.resolve(SRC, "obj1") == HA
    cap, _ = _cap([row(HA, "present", 1), row(HA, "withdrawn", 2)])
    assert cap.resolve(SRC, "obj1") is None
    # row order must not matter
    cap, _ = _cap([row(HA, "withdrawn", 2), row(HA, "present", 1)])
    assert cap.resolve(SRC, "obj1") is None


def test_resolve_historical_withdrawn_mapping_does_not_cause_ambiguity():
    cap, _ = _cap(
        [row(HA, "present", 1), row(HB, "present", 2), row(HA, "withdrawn", 3)]
    )
    assert cap.resolve(SRC, "obj1") == HB


def test_resolve_counts_pairs_so_cross_type_collision_raises():
    cap, _ = _cap(
        [row(HA, "present", 1, etype="genome"), row(HA, "present", 1, etype="protein")]
    )
    with pytest.raises(AliasAmbiguousError):
        cap.resolve(SRC, "obj1")


@pytest.mark.parametrize(
    "src", [s for s in ALIAS_SOURCES if s not in INVERTIBLE_ALIAS_SOURCES]
)
def test_resolve_refuses_non_invertible_source_even_for_one_row(src):
    assert src in ("ncbi_taxon", "arc_analysis")
    cap, fake = _cap([row(HA, "present", 1, src=src, aid="9606")])
    with pytest.raises(AliasNotInvertibleError) as ei:
        cap.resolve(src, "9606")
    assert type(ei.value) is not AliasAmbiguousError
    assert "aliases()" in str(ei.value)
    assert fake.sql == []  # refused from the contract, before any query
