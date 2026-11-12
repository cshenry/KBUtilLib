"""Tests for ``register_aliases`` / ``observe_alias`` (the alias write boundary).

Driven against a stubbed write capability that records ``load()`` calls and
answers the existence probe and the fill-only lookup from an in-memory store.
"""

from __future__ import annotations

import re

import pytest

from kbutillib.domains.kbase.berdl.capability import BerdlLoadRefusedError
from kbutillib.domains.kbase.berdl.clearinghouse_capability import (
    ALIAS_LATEST_ORDER_BY,
    ClearinghouseCapability,
    ClearinghouseLoadPostflightError,
    ClearinghouseSchemaPreflightError,
    ClearinghouseWriteTargetMismatchError,
    latest_alias_observations,
)
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    ALIAS_SOURCES,
    column_names,
    table_name,
)

from .test_clearinghouse_capability import _FakeWriteCapability

H = "ab" * 32
MD5 = "0123456789abcdef0123456789abcdef"
TABLE = table_name("genome", "alias")


class _AliasFake(_FakeWriteCapability):
    def __init__(self, *, table_exists=True, live_columns=None, **kw):
        super().__init__(**kw)
        self.table_exists = table_exists
        self.live_columns = (
            list(live_columns)
            if live_columns is not None
            else list(column_names("genome", "alias"))
        )
        self.stored: list[dict] = []

    def load(self, **kwargs):
        result = super().load(**kwargs)
        for name, rows in kwargs["dataframes"].items():
            if name == TABLE:
                self.stored.extend(rows)
        return result

    def query(self, sql, *, params=None, engine=None, **kwargs):
        if sql.startswith("DESCRIBE"):
            self.queries.append(sql)
            if not self.table_exists:
                raise RuntimeError("TABLE_OR_VIEW_NOT_FOUND")
            return [{"col_name": c} for c in self.live_columns]
        if sql.startswith("SELECT DISTINCT entity_type, entity_hash"):
            self.queries.append(sql)
            return [
                {
                    k: r[k]
                    for k in (
                        "entity_type",
                        "entity_hash",
                        "alias_source",
                        "alias_id",
                        "observation",
                    )
                }
                for r in self.stored
            ]
        return super().query(sql, params=params, engine=engine, **kwargs)


def _cap(**kw):
    fake = _AliasFake(**kw)
    return ClearinghouseCapability(capability=fake), fake


def _row(**over):
    row = {"entity_hash": H, "alias_source": "kbase_assembly_md5", "alias_id": MD5}
    row.update(over)
    return row


def test_invalid_row_refuses_whole_call_naming_index_with_load_uncalled():
    cap, fake = _cap()
    with pytest.raises(ValueError, match=r"alias row 1"):
        cap.register_aliases("genome", [_row(), _row(alias_id="not-md5")])
    assert len(fake.load_calls) == 0


def test_identical_retry_makes_no_load_call_and_reports_already_present():
    cap, fake = _cap()
    first = cap.register_aliases("genome", [_row(), _row(alias_id="f" * 32)])
    assert len(fake.load_calls) == 1
    assert len(first["written"]) == 2
    second = cap.register_aliases("genome", [_row(), _row(alias_id="f" * 32)])
    assert len(fake.load_calls) == 1  # no second load
    assert second["load_result"] is None
    assert second["written"] == []
    assert len(second["already_present"]) == 2


def test_misspelled_source_refused_naming_legal_values():
    cap, fake = _cap()
    with pytest.raises(ValueError) as ei:
        cap.register_aliases("genome", [_row(alias_source="ncbi_assemblyy")])
    assert all(s in str(ei.value) for s in ALIAS_SOURCES)
    assert len(fake.load_calls) == 0


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_empty_whitespace_none_alias_id_refused(bad):
    cap, fake = _cap()
    with pytest.raises(ValueError, match=r"alias row 0"):
        cap.register_aliases("genome", [_row(alias_source="arc_record", alias_id=bad)])
    assert len(fake.load_calls) == 0


def test_alias_id_with_whitespace_written_verbatim():
    cap, fake = _cap()
    cap.register_aliases(
        "genome", [_row(alias_source="arc_record", alias_id=" rec 1 ")]
    )
    assert fake.load_calls[0]["dataframes"][TABLE][0]["alias_id"] == " rec 1 "


def test_offpod_refuses_before_any_transport_call():
    cap, fake = _cap(locus="off_pod")
    with pytest.raises(BerdlLoadRefusedError):
        cap.register_aliases("genome", [_row()])
    with pytest.raises(BerdlLoadRefusedError):
        cap.observe_alias("genome", H, "arc_record", "r", "withdrawn")
    assert fake.load_calls == [] and fake.queries == []


def test_null_row_count_is_failed_load():
    cap, _ = _cap(postflight_row_count=None)
    with pytest.raises(ClearinghouseLoadPostflightError):
        cap.register_aliases("genome", [_row()])
    cap, _ = _cap(postflight_row_count=None)
    with pytest.raises(ClearinghouseLoadPostflightError):
        cap.observe_alias("genome", H, "arc_record", "r", "withdrawn")


def test_write_target_mismatch_refuses_both_verbs():
    cap, fake = _cap(probe_ns="elsewhere")
    with pytest.raises(ClearinghouseWriteTargetMismatchError):
        cap.register_aliases("genome", [_row()])
    with pytest.raises(ClearinghouseWriteTargetMismatchError):
        cap.observe_alias("genome", H, "arc_record", "r", "withdrawn")
    assert len(fake.load_calls) == 0


def test_register_aliases_writes_present_without_being_told():
    cap, fake = _cap()
    cap.register_aliases("genome", [_row()])
    assert fake.stored[0]["observation"] == "present"


def test_register_aliases_refuses_withdrawn_observation():
    cap, fake = _cap()
    with pytest.raises(ValueError, match="observe_alias"):
        cap.register_aliases("genome", [_row(observation="withdrawn")])
    assert len(fake.load_calls) == 0


def test_invalid_observation_refused_with_load_uncalled():
    cap, fake = _cap()
    with pytest.raises(ValueError, match="observation"):
        cap.observe_alias("genome", H, "arc_record", "r", "deleted")
    assert len(fake.load_calls) == 0


def test_present_withdrawn_present_appends_all_three():
    cap, fake = _cap()
    cap.register_aliases("genome", [_row(alias_source="arc_record", alias_id="r")])
    cap.observe_alias("genome", H, "arc_record", "r", "withdrawn")
    cap.observe_alias("genome", H, "arc_record", "r", "present")
    assert len(fake.load_calls) == 3
    assert [r["observation"] for r in fake.stored] == [
        "present",
        "withdrawn",
        "present",
    ]


def test_observe_alias_appends_identical_repeat():
    cap, fake = _cap()
    cap.observe_alias("genome", H, "arc_record", "r", "present")
    cap.observe_alias("genome", H, "arc_record", "r", "present")
    assert len(fake.load_calls) == 2


@pytest.mark.parametrize("col", ["observed_at", "ingest_batch_id"])
def test_caller_supplied_clock_columns_refused_by_register_aliases(col):
    cap, fake = _cap()
    with pytest.raises(ValueError, match=col):
        cap.register_aliases("genome", [_row(**{col: "x"})])
    assert len(fake.load_calls) == 0


def test_observe_alias_has_no_clock_parameters():
    # observe_alias takes no row mapping, so a caller-supplied clock value
    # cannot reach it: passing one is a TypeError, never silently used.
    cap, fake = _cap()
    with pytest.raises(TypeError):
        cap.observe_alias(
            "genome", H, "arc_record", "r", "present", observed_at="2020-01-01"
        )
    with pytest.raises(TypeError):
        cap.observe_alias(
            "genome", H, "arc_record", "r", "present", ingest_batch_id="x"
        )
    assert len(fake.load_calls) == 0


def test_clock_is_generated_utc_and_fresh_ulid_per_call():
    cap, fake = _cap()
    cap.register_aliases("genome", [_row(), _row(alias_id="f" * 32)])
    cap.observe_alias("genome", H, "arc_record", "r", "withdrawn")
    a, b, c = fake.stored
    assert a["ingest_batch_id"] == b["ingest_batch_id"] != c["ingest_batch_id"]
    assert re.fullmatch(r"[0-9A-Z]{26}", a["ingest_batch_id"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+Z", a["observed_at"])


def test_row_entity_type_disagreeing_with_argument_refused():
    cap, fake = _cap()
    with pytest.raises(ValueError, match="entity_type"):
        cap.register_aliases("genome", [_row(entity_type="gene")])
    assert len(fake.load_calls) == 0


def test_routes_to_type_table_and_projects_standard_columns():
    cap, fake = _cap()
    cap.register_aliases("gene", [_row(alias_source="arc_record", alias_id="r")])
    call = fake.load_calls[0]
    assert call["tables"] == [{"name": table_name("gene", "alias"), "mode": "append"}]
    row = call["dataframes"][table_name("gene", "alias")][0]
    assert list(row) == list(column_names("gene", "alias"))


def test_absent_table_refused_naming_op_c5_with_load_uncalled():
    cap, fake = _cap(table_exists=False)
    with pytest.raises(ClearinghouseSchemaPreflightError, match="OP-C5"):
        cap.register_aliases("genome", [_row()])
    with pytest.raises(ClearinghouseSchemaPreflightError, match="OP-C5"):
        cap.observe_alias("genome", H, "arc_record", "r", "withdrawn")
    assert len(fake.load_calls) == 0


def test_table_missing_a_column_refused_naming_op_c5():
    cap, fake = _cap(
        live_columns=[
            "entity_hash",
            "entity_type",
            "alias_source",
            "alias_id",
            "observation",
        ]
    )
    with pytest.raises(ClearinghouseSchemaPreflightError, match="OP-C5"):
        cap.register_aliases("genome", [_row()])
    assert len(fake.load_calls) == 0


def test_neither_verb_calls_assert_lake5_schema(monkeypatch):
    cap, fake = _cap()

    def boom(*a, **k):
        raise AssertionError("_assert_lake5_schema must not be called")

    monkeypatch.setattr(cap, "_assert_lake5_schema", boom)
    cap.register_aliases("genome", [_row()])
    cap.observe_alias("genome", H, "arc_record", "r", "withdrawn")


def test_tie_order_withdrawn_wins_exact_tie_stably():
    assert ALIAS_LATEST_ORDER_BY == (
        "observed_at DESC, ingest_batch_id DESC, observation DESC"
    )
    base = {
        "entity_type": "genome",
        "entity_hash": H,
        "alias_source": "arc_record",
        "alias_id": "r",
        "observed_at": "2026-01-01T00:00:00.000000Z",
        "ingest_batch_id": "01ABC",
    }
    present = {**base, "observation": "present"}
    withdrawn = {**base, "observation": "withdrawn"}
    for _ in range(3):
        for rows in ([present, withdrawn], [withdrawn, present]):
            (win,) = latest_alias_observations(rows).values()
            assert win["observation"] == "withdrawn"
    later = {**present, "observed_at": "2026-01-02T00:00:00.000000Z"}
    (win,) = latest_alias_observations([withdrawn, later]).values()
    assert win["observation"] == "present"
