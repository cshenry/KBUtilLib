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
from kbutillib.domains.kbase.berdl.clearinghouse_parity_fixture import (
    DUPLICATE_COLLAPSE,
    INGEST_BATCH_ID_TIE_BREAK,
    NEWEST_WINS,
    RESULT_TYPE_VERSION_OUTSIDE_SLOT_KEY,
    SOURCE_ISOLATION,
    TERM_REMOVAL,
)

_TABLE_FQN = "result"

_CREATE_RESULT_TABLE = """
CREATE TABLE result (
    entity_hash BLOB,
    entity_type VARCHAR,
    result_type VARCHAR,
    source VARCHAR,
    result_type_version VARCHAR,
    payload VARCHAR,
    observed_at TIMESTAMP,
    ingest_batch_id VARCHAR
)
"""

_INSERT_ROW = "INSERT INTO result VALUES (?, ?, ?, ?, ?, ?, ?, ?)"


def _for_duckdb(sql: str) -> str:
    """Translate Spark-style backtick quoting to DuckDB's double quotes.

    Both are quoted-identifier delimiters for the same concept; DuckDB
    simply picked the other ANSI-legal character. See the module
    docstring.
    """
    return sql.replace("`", '"')


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
        # Not yet part of the shared parity fixture rows (that is a
        # separate, later task) -- default every row inserted through
        # this harness to "protein" so this file's own tests keep
        # exercising the pre-existing three-column slot key/derivation
        # logic unchanged while the table's declared column set grows.
        entity_type: str = "protein",
    ) -> None:
        # Bind the BLOB entity_hash as a parameter -- never a literal.
        self.con.execute(
            _INSERT_ROW,
            [
                entity_hash,
                entity_type,
                result_type,
                source,
                result_type_version,
                json.dumps(payload),
                observed_at,
                ingest_batch_id,
            ],
        )

    def current_state(
        self,
        *,
        sources: list[str] | None = None,
        entity_types: list[str] | None = None,
    ) -> list[dict]:
        sql = current_state_sql(_TABLE_FQN, sources=sources, entity_types=entity_types)
        columns = [
            "entity_hash",
            "entity_type",
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
    """Property 1: duplicate identical appends collapse to exactly one row.

    Rows come from :mod:`kbutillib.domains.kbase.berdl.clearinghouse_parity_fixture`
    -- the same fixture the in-pod OP3 parity check appends to the real
    ``result`` table -- rather than being re-typed here, so the two
    cannot drift apart.
    """

    def test_three_byte_identical_appends_yield_one_current_row(self, fixture):
        case = DUPLICATE_COLLAPSE
        for row in case.rows:
            fixture.insert(**row)
        rows = [
            row
            for row in fixture.current_state()
            if row["entity_hash"] == case.entity_hash
        ]
        assert len(rows) == 1


class TestNewestWins:
    """Property 2: the newest row (by observed_at) wins per slot."""

    def test_later_observed_at_row_is_the_current_one(self, fixture):
        case = NEWEST_WINS
        for row in case.rows:
            fixture.insert(**row)
        rows = [
            row
            for row in fixture.current_state()
            if row["entity_hash"] == case.entity_hash
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
        case = INGEST_BATCH_ID_TIE_BREAK
        rng = random.Random(shuffle_seed)
        order = list(case.rows)
        rng.shuffle(order)
        for row in order:
            fixture.insert(**row)

        rows = [
            row
            for row in fixture.current_state()
            if row["entity_hash"] == case.entity_hash
        ]
        winning_row = max(case.rows, key=lambda row: row["ingest_batch_id"])
        assert len(rows) == 1
        assert rows[0]["ingest_batch_id"] == winning_row["ingest_batch_id"]
        assert json.loads(rows[0]["payload"]) == winning_row["payload"]


class TestRemovalProperty:
    """Property 4: a term absent from the newer whole-call payload is absent
    from current state -- results are one row per call, not one row per
    term, so a superseded payload's extra keys do not survive.
    """

    def test_key_dropped_in_newer_payload_is_absent_from_current_state(
        self, fixture
    ):
        case = TERM_REMOVAL
        for row in case.rows:
            fixture.insert(**row)
        rows = [
            row
            for row in fixture.current_state()
            if row["entity_hash"] == case.entity_hash
        ]
        assert len(rows) == 1
        current_payload = json.loads(rows[0]["payload"])
        assert "ec" not in current_payload
        assert current_payload == {"go": {"term": "GO:0001"}}


class TestSourceIsolation:
    """Property 5: rows from one source never shadow another source's row
    for the same entity_hash/result_type -- both tools stay current -- and
    a ``sources`` filter prunes to exactly the requested sources.
    """

    def test_two_tools_annotating_same_entity_both_remain_current(self, fixture):
        case = SOURCE_ISOLATION
        for row in case.rows:
            fixture.insert(**row)
        rows = {
            row["source"]: row
            for row in fixture.current_state()
            if row["entity_hash"] == case.entity_hash
        }
        source_a, source_b = case.sources
        assert set(rows) == {source_a, source_b}
        assert json.loads(rows[source_a]["payload"]) == {"ns": {"k": "from_a"}}
        assert json.loads(rows[source_b]["payload"]) == {"ns": {"k": "from_b"}}

    def test_sources_filter_prunes_to_requested_sources_only(self, fixture):
        case = SOURCE_ISOLATION
        for row in case.rows:
            fixture.insert(**row)
        source_a = case.sources[0]
        rows = [
            row
            for row in fixture.current_state(sources=[source_a])
            if row["entity_hash"] == case.entity_hash
        ]
        assert {row["source"] for row in rows} == {source_a}


class TestEntityTypeInSlotKey:
    """Property 7: entity_type is part of the slot key, not entity_hash alone.

    ``_standardize_protein`` and ``_standardize_gene_dna`` are the same
    standardizer (a bare ``_clean_sequence_letters``), so a sequence over
    the alphabet {A,C,G,T,N} produces a byte-identical ``entity_hash``
    whether submitted as a protein or as gene DNA. A protein row and a
    gene_dna row that share an entity_hash, result_type and source must
    therefore occupy TWO distinct slots -- both must survive as current,
    never collapsed to one by a window function partitioned on
    entity_hash/result_type/source alone.
    """

    def test_protein_and_gene_dna_sharing_hash_both_survive(self, fixture):
        entity_hash = b"shared-hash-bytes".ljust(32, b"\x00")
        shared_kwargs = dict(
            entity_hash=entity_hash,
            result_type="annotation",
            source="parity-check/entity_type_slot_key/toolA",
            result_type_version="v1",
            observed_at="2026-01-01 00:00:00",
            ingest_batch_id="01HENTITYTYPESLOTKEY0000AA",
        )
        fixture.insert(
            entity_type="protein",
            payload={"kind": "protein"},
            **shared_kwargs,
        )
        fixture.insert(
            entity_type="gene_dna",
            payload={"kind": "gene_dna"},
            **shared_kwargs,
        )
        rows = {
            row["entity_type"]: row
            for row in fixture.current_state()
            if row["entity_hash"] == entity_hash
        }
        assert set(rows) == {"protein", "gene_dna"}
        assert json.loads(rows["protein"]["payload"]) == {"kind": "protein"}
        assert json.loads(rows["gene_dna"]["payload"]) == {"kind": "gene_dna"}

    def test_entity_types_filter_prunes_to_requested_types_only(self, fixture):
        entity_hash = b"shared-hash-bytes".ljust(32, b"\x00")
        shared_kwargs = dict(
            entity_hash=entity_hash,
            result_type="annotation",
            source="parity-check/entity_type_slot_key/toolA",
            result_type_version="v1",
            observed_at="2026-01-01 00:00:00",
            ingest_batch_id="01HENTITYTYPESLOTKEY0000AB",
        )
        fixture.insert(
            entity_type="protein",
            payload={"kind": "protein"},
            **shared_kwargs,
        )
        fixture.insert(
            entity_type="gene_dna",
            payload={"kind": "gene_dna"},
            **shared_kwargs,
        )
        rows = [
            row
            for row in fixture.current_state(entity_types=["protein"])
            if row["entity_hash"] == entity_hash
        ]
        assert {row["entity_type"] for row in rows} == {"protein"}

    def test_entity_types_and_sources_filters_combine_with_and(self, fixture):
        entity_hash = b"shared-hash-bytes".ljust(32, b"\x00")
        base = dict(
            entity_hash=entity_hash,
            result_type="annotation",
            result_type_version="v1",
            observed_at="2026-01-01 00:00:00",
        )
        # (protein, sourceA), (protein, sourceB), (gene_dna, sourceA)
        fixture.insert(
            entity_type="protein",
            source="parity-check/entity_type_and_source/toolA",
            payload={"kind": "protein_a"},
            ingest_batch_id="01HENTITYTYPEANDSOURCE0AA",
            **base,
        )
        fixture.insert(
            entity_type="protein",
            source="parity-check/entity_type_and_source/toolB",
            payload={"kind": "protein_b"},
            ingest_batch_id="01HENTITYTYPEANDSOURCE0AB",
            **base,
        )
        fixture.insert(
            entity_type="gene_dna",
            source="parity-check/entity_type_and_source/toolA",
            payload={"kind": "gene_dna_a"},
            ingest_batch_id="01HENTITYTYPEANDSOURCE0AC",
            **base,
        )
        rows = [
            row
            for row in fixture.current_state(
                sources=["parity-check/entity_type_and_source/toolA"],
                entity_types=["protein"],
            )
            if row["entity_hash"] == entity_hash
        ]
        assert len(rows) == 1
        assert rows[0]["entity_type"] == "protein"
        assert rows[0]["source"] == "parity-check/entity_type_and_source/toolA"

    def test_empty_entity_types_filters_out_everything(self, fixture):
        entity_hash = b"shared-hash-bytes".ljust(32, b"\x00")
        fixture.insert(
            entity_hash=entity_hash,
            entity_type="protein",
            result_type="annotation",
            source="parity-check/entity_type_empty/toolA",
            result_type_version="v1",
            payload={"kind": "protein"},
            observed_at="2026-01-01 00:00:00",
            ingest_batch_id="01HENTITYTYPEEMPTY0000AA",
        )
        rows = fixture.current_state(entity_types=[])
        assert rows == []

    def test_empty_sources_with_entity_types_still_filters_out_everything(
        self, fixture
    ):
        entity_hash = b"shared-hash-bytes".ljust(32, b"\x00")
        fixture.insert(
            entity_hash=entity_hash,
            entity_type="protein",
            result_type="annotation",
            source="parity-check/entity_type_empty/toolA",
            result_type_version="v1",
            payload={"kind": "protein"},
            observed_at="2026-01-01 00:00:00",
            ingest_batch_id="01HENTITYTYPEEMPTY0000AB",
        )
        rows = fixture.current_state(sources=[], entity_types=["protein"])
        assert rows == []


class TestResultTypeVersionOutsideSlotKey:
    """Property 6: rows differing only in result_type_version still
    collapse to one current row -- the version is not part of the slot
    key, so it never forks a slot.
    """

    def test_slot_with_only_version_difference_collapses_to_one_row(self, fixture):
        case = RESULT_TYPE_VERSION_OUTSIDE_SLOT_KEY
        for row in case.rows:
            fixture.insert(**row)
        rows = [
            row
            for row in fixture.current_state()
            if row["entity_hash"] == case.entity_hash
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

    def test_partition_by_names_all_four_slot_key_columns_in_order(self):
        sql = current_state_sql("ns.result")
        assert (
            "PARTITION BY entity_hash, entity_type, result_type, source" in sql
        )

    def test_entity_types_filter_renders_as_where_in(self):
        sql = current_state_sql("ns.result", entity_types=["protein", "gene_dna"])
        assert "WHERE entity_type IN ('protein', 'gene_dna')" in sql

    def test_empty_entity_types_list_filters_out_everything(self):
        sql = current_state_sql("ns.result", entity_types=[])
        assert "WHERE 1 = 0" in sql

    def test_no_entity_types_filter_when_none(self):
        sql = current_state_sql("ns.result")
        assert "entity_type IN" not in sql

    def test_sources_and_entity_types_combine_with_and(self):
        sql = current_state_sql(
            "ns.result", sources=["toolA/1"], entity_types=["protein"]
        )
        assert (
            "WHERE source IN ('toolA/1') AND entity_type IN ('protein')" in sql
        )
        assert " OR " not in sql

    def test_function_returns_plain_text_with_no_i_o(self):
        # Calling it twice with the same args is byte-identical -- pure.
        assert current_state_sql("ns.result") == current_state_sql("ns.result")
