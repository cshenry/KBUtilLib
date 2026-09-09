"""Tests for kbutillib.domains.kbase.berdl.clearinghouse_derivation.

These tests EXECUTE the SQL that :func:`current_state_sql` returns
against DuckDB rather than merely asserting on the SQL string, because
the property under test -- "which row wins a slot" -- is a statement
about query execution, not about text shape.

DuckDB is a SURROGATE engine here: it proves the *logic* of the
derivation (dedup, tie-break, slot-key scoping), not the Spark-on-Iceberg
dialect the query runs under in production. Proving the SQL text is also
valid Spark-on-Iceberg SQL is an in-pod operator parity check, out of
scope for this off-pod suite.

One accommodation is needed to run this dialect-authoritative (Spark)
SQL against the surrogate (DuckDB): DuckDB has no backtick-quoted
identifier syntax at all (it uses ANSI double quotes), while Spark uses
backticks. ``_for_duckdb`` below translates *only* that quoting
character, leaving every other token -- the window function, the
PARTITION BY/ORDER BY columns, the WHERE clause -- untouched. That is a
syntax-only translation between two quoted-identifier conventions, not a
change to the logic under test.
"""

from __future__ import annotations

import json
import random

import duckdb
import pytest

from kbutillib.domains.kbase.berdl.clearinghouse_derivation import current_state_sql

_TABLE_FQN = "result"

_CREATE_RESULT_TABLE = """
CREATE TABLE result (
    entity_hash BLOB,
    result_type VARCHAR,
    source VARCHAR,
    result_type_version VARCHAR,
    payload VARCHAR,
    observed_at TIMESTAMP,
    ingest_batch_id VARCHAR
)
"""

_INSERT_ROW = "INSERT INTO result VALUES (?, ?, ?, ?, ?, ?, ?)"


def _for_duckdb(sql: str) -> str:
    """Translate Spark-style backtick quoting to DuckDB's double quotes.

    Both are quoted-identifier delimiters for the same concept; DuckDB
    simply picked the other ANSI-legal character. See the module
    docstring.
    """
    return sql.replace("`", '"')


def _hash(tag: str) -> bytes:
    """A deterministic 32-byte stand-in for a real sha256 entity_hash."""
    return tag.encode("ascii").ljust(32, b"\x00")[:32]


class ResultFixture:
    """A tiny in-memory DuckDB ``result`` table plus a row-inserting helper."""

    def __init__(self) -> None:
        self.con = duckdb.connect()
        self.con.execute(_CREATE_RESULT_TABLE)

    def insert(
        self,
        *,
        entity_hash: bytes,
        result_type: str,
        source: str,
        result_type_version: str,
        payload: dict,
        observed_at: str,
        ingest_batch_id: str,
    ) -> None:
        # Bind the BLOB entity_hash as a parameter -- never a literal.
        self.con.execute(
            _INSERT_ROW,
            [
                entity_hash,
                result_type,
                source,
                result_type_version,
                json.dumps(payload),
                observed_at,
                ingest_batch_id,
            ],
        )

    def current_state(self, *, sources: list[str] | None = None) -> list[dict]:
        sql = current_state_sql(_TABLE_FQN, sources=sources)
        columns = [
            "entity_hash",
            "result_type",
            "source",
            "result_type_version",
            "payload",
            "observed_at",
            "ingest_batch_id",
        ]
        rows = self.con.execute(_for_duckdb(sql)).fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]


@pytest.fixture
def fixture() -> ResultFixture:
    return ResultFixture()


class TestDuplicateAppendsCollapse:
    """Property 1: duplicate identical appends collapse to exactly one row."""

    def test_three_byte_identical_appends_yield_one_current_row(self, fixture):
        entity_hash = _hash("dup")
        for i in range(3):
            fixture.insert(
                entity_hash=entity_hash,
                result_type="annotation",
                source="toolA/1",
                result_type_version="v1",
                payload={"ns": {"k": "same"}},
                observed_at=f"2026-01-01 00:0{i}:00",
                ingest_batch_id=f"01H00000000000000000000{i}",
            )
        rows = [
            row
            for row in fixture.current_state()
            if row["entity_hash"] == entity_hash
        ]
        assert len(rows) == 1


class TestNewestWins:
    """Property 2: the newest row (by observed_at) wins per slot."""

    def test_later_observed_at_row_is_the_current_one(self, fixture):
        entity_hash = _hash("newest")
        fixture.insert(
            entity_hash=entity_hash,
            result_type="annotation",
            source="toolA/1",
            result_type_version="v1",
            payload={"ns": {"k": "old"}},
            observed_at="2026-01-01 00:00:00",
            ingest_batch_id="01H0000000000000000000AA",
        )
        fixture.insert(
            entity_hash=entity_hash,
            result_type="annotation",
            source="toolA/1",
            result_type_version="v1",
            payload={"ns": {"k": "new"}},
            observed_at="2026-01-02 00:00:00",
            ingest_batch_id="01H0000000000000000000AB",
        )
        rows = [
            row
            for row in fixture.current_state()
            if row["entity_hash"] == entity_hash
        ]
        assert len(rows) == 1
        assert json.loads(rows[0]["payload"]) == {"ns": {"k": "new"}}


class TestIngestBatchIdTieBreak:
    """Property 3: an observed_at tie is broken by the greater ingest_batch_id.

    ingest_batch_id is a ULID: lexicographically greater means later.
    Both insertion orders are exercised so the test cannot pass by
    accident of insertion order.
    """

    @pytest.mark.parametrize("shuffle_seed", [1, 2, 3, 4, 5])
    def test_greater_ingest_batch_id_wins_regardless_of_insertion_order(
        self, fixture, shuffle_seed
    ):
        entity_hash = _hash("tie")
        rows_to_insert = [
            {
                "entity_hash": entity_hash,
                "result_type": "annotation",
                "source": "toolA/1",
                "result_type_version": "v1",
                "payload": {"ns": {"k": "lower_batch"}},
                "observed_at": "2026-01-01 00:00:00",
                "ingest_batch_id": "01H0000000000000000000AA",
            },
            {
                "entity_hash": entity_hash,
                "result_type": "annotation",
                "source": "toolA/1",
                "result_type_version": "v1",
                "payload": {"ns": {"k": "higher_batch"}},
                "observed_at": "2026-01-01 00:00:00",  # identical timestamp
                "ingest_batch_id": "01H0000000000000000000AZ",
            },
        ]
        rng = random.Random(shuffle_seed)
        order = rows_to_insert[:]
        rng.shuffle(order)
        for row in order:
            fixture.insert(**row)

        rows = [
            row
            for row in fixture.current_state()
            if row["entity_hash"] == entity_hash
        ]
        assert len(rows) == 1
        assert rows[0]["ingest_batch_id"] == "01H0000000000000000000AZ"
        assert json.loads(rows[0]["payload"]) == {"ns": {"k": "higher_batch"}}


class TestRemovalProperty:
    """Property 4: a term absent from the newer whole-call payload is absent
    from current state -- results are one row per call, not one row per
    term, so a superseded payload's extra keys do not survive.
    """

    def test_key_dropped_in_newer_payload_is_absent_from_current_state(
        self, fixture
    ):
        entity_hash = _hash("removal")
        fixture.insert(
            entity_hash=entity_hash,
            result_type="annotation",
            source="toolA/1",
            result_type_version="v1",
            payload={"go": {"term": "GO:0001"}, "ec": {"term": "1.1.1.1"}},
            observed_at="2026-01-01 00:00:00",
            ingest_batch_id="01H0000000000000000000BA",
        )
        fixture.insert(
            entity_hash=entity_hash,
            result_type="annotation",
            source="toolA/1",
            result_type_version="v1",
            payload={"go": {"term": "GO:0001"}},  # 'ec' namespace dropped
            observed_at="2026-01-02 00:00:00",
            ingest_batch_id="01H0000000000000000000BB",
        )
        rows = [
            row
            for row in fixture.current_state()
            if row["entity_hash"] == entity_hash
        ]
        assert len(rows) == 1
        current_payload = json.loads(rows[0]["payload"])
        assert "ec" not in current_payload
        assert current_payload == {"go": {"term": "GO:0001"}}


class TestSourceIsolation:
    """Property 5: rows from one source never shadow another source's row
    for the same entity_hash/result_type -- both tools stay current.
    """

    def test_two_tools_annotating_same_entity_both_remain_current(self, fixture):
        entity_hash = _hash("isolation")
        fixture.insert(
            entity_hash=entity_hash,
            result_type="annotation",
            source="toolA/1",
            result_type_version="v1",
            payload={"ns": {"k": "from_a"}},
            observed_at="2026-01-01 00:00:00",
            ingest_batch_id="01H0000000000000000000CA",
        )
        fixture.insert(
            entity_hash=entity_hash,
            result_type="annotation",
            source="toolB/1",
            result_type_version="v1",
            payload={"ns": {"k": "from_b"}},
            observed_at="2026-01-01 00:00:00",
            ingest_batch_id="01H0000000000000000000CA",
        )
        rows = {
            row["source"]: row
            for row in fixture.current_state()
            if row["entity_hash"] == entity_hash
        }
        assert set(rows) == {"toolA/1", "toolB/1"}
        assert json.loads(rows["toolA/1"]["payload"]) == {"ns": {"k": "from_a"}}
        assert json.loads(rows["toolB/1"]["payload"]) == {"ns": {"k": "from_b"}}

    def test_sources_filter_prunes_to_requested_sources_only(self, fixture):
        entity_hash = _hash("prune")
        fixture.insert(
            entity_hash=entity_hash,
            result_type="annotation",
            source="toolA/1",
            result_type_version="v1",
            payload={"ns": {"k": "from_a"}},
            observed_at="2026-01-01 00:00:00",
            ingest_batch_id="01H0000000000000000000DA",
        )
        fixture.insert(
            entity_hash=entity_hash,
            result_type="annotation",
            source="toolB/1",
            result_type_version="v1",
            payload={"ns": {"k": "from_b"}},
            observed_at="2026-01-01 00:00:00",
            ingest_batch_id="01H0000000000000000000DA",
        )
        rows = [
            row
            for row in fixture.current_state(sources=["toolA/1"])
            if row["entity_hash"] == entity_hash
        ]
        assert {row["source"] for row in rows} == {"toolA/1"}


class TestResultTypeVersionOutsideSlotKey:
    """Property 6: rows differing only in result_type_version still
    collapse to one current row -- the version is not part of the slot
    key, so it never forks a slot.
    """

    def test_slot_with_only_version_difference_collapses_to_one_row(self, fixture):
        entity_hash = _hash("version")
        fixture.insert(
            entity_hash=entity_hash,
            result_type="annotation",
            source="toolA/1",
            result_type_version="v1",
            payload={"ns": {"k": "old_version"}},
            observed_at="2026-01-01 00:00:00",
            ingest_batch_id="01H0000000000000000000EA",
        )
        fixture.insert(
            entity_hash=entity_hash,
            result_type="annotation",
            source="toolA/1",
            result_type_version="v2",  # schema bump, same slot key
            payload={"ns": {"k": "new_version"}},
            observed_at="2026-01-02 00:00:00",
            ingest_batch_id="01H0000000000000000000EB",
        )
        rows = [
            row
            for row in fixture.current_state()
            if row["entity_hash"] == entity_hash
        ]
        assert len(rows) == 1
        assert rows[0]["result_type_version"] == "v2"
        assert json.loads(rows[0]["payload"]) == {"ns": {"k": "new_version"}}


class TestDialectConformance:
    """String-shape checks on the emitted SQL text for the dialect
    constraints that DuckDB execution alone cannot prove (DuckDB accepts
    a superset of Spark-legal syntax, so passing execution does not by
    itself prove the SQL stays inside the DuckDB/Spark intersection).
    """

    def test_fqn_segments_are_each_backtick_quoted(self):
        sql = current_state_sql("kbaseincubator.clearinghouse.result")
        assert "`kbaseincubator`.`clearinghouse`.`result`" in sql

    def test_already_quoted_segment_is_not_double_quoted(self):
        sql = current_state_sql("`kbaseincubator`.clearinghouse.result")
        assert "``kbaseincubator``" not in sql
        assert "`kbaseincubator`.`clearinghouse`.`result`" in sql

    def test_sources_filter_renders_as_where_in(self):
        sql = current_state_sql("ns.result", sources=["toolA/1", "toolB/2"])
        assert "WHERE source IN ('toolA/1', 'toolB/2')" in sql

    def test_source_value_with_quote_is_escaped(self):
        sql = current_state_sql("ns.result", sources=["weird'source"])
        assert "'weird''source'" in sql

    def test_empty_sources_list_filters_out_everything(self):
        sql = current_state_sql("ns.result", sources=[])
        assert "WHERE 1 = 0" in sql

    def test_no_sources_filter_when_none(self):
        sql = current_state_sql("ns.result")
        assert "source IN" not in sql
        assert "1 = 0" not in sql

    def test_no_duckdb_blob_literal_syntax(self):
        sql = current_state_sql("ns.result")
        assert "BLOB" not in sql.upper()
        assert "X'" not in sql

    def test_no_nulls_first_or_last(self):
        sql = current_state_sql("ns.result")
        assert "NULLS" not in sql.upper()

    def test_function_returns_plain_text_with_no_i_o(self):
        # Calling it twice with the same args is byte-identical -- pure.
        assert current_state_sql("ns.result") == current_state_sql("ns.result")
