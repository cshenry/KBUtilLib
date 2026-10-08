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

from kbutillib.domains.identity import (
    DEFAULT_PARAMETER_SET_HASH,
    parameter_set_hash,
)
from kbutillib.domains.kbase.berdl.clearinghouse_derivation import current_state_sql
from kbutillib.domains.kbase.berdl.clearinghouse_parity_fixture import (
    DUPLICATE_COLLAPSE,
    INGEST_BATCH_ID_TIE_BREAK,
    NEWEST_WINS,
    PARAMETER_SET_EVALUE_HASH,
    PARAMETER_SET_EXPECTED_PAYLOADS,
    PARAMETER_SET_FORKS_SLOT,
    RESULT_TYPE_VERSION_OUTSIDE_SLOT_KEY,
    SOURCE_ISOLATION,
    TERM_REMOVAL,
    fixture_entity_hash,
)

_TABLE_FQN = "result"

#: The nine declared result columns, in DDL order -- the same order
#: ``clearinghouse_schema._RESULT_COLUMNS`` declares, with
#: ``parameter_set_hash`` immediately after ``source``. The surrogate table
#: below, its positional INSERT and the projection in
#: :meth:`ResultFixture.current_state` are all built from this one list, so
#: the next column added to the schema cannot silently misalign them.
_RESULT_COLUMN_ORDER = (
    "entity_hash",
    "entity_type",
    "result_type",
    "source",
    "parameter_set_hash",
    "result_type_version",
    "payload",
    "observed_at",
    "ingest_batch_id",
)

_CREATE_RESULT_TABLE = """
CREATE TABLE result (
    entity_hash VARCHAR,
    entity_type VARCHAR,
    result_type VARCHAR,
    source VARCHAR,
    parameter_set_hash VARCHAR,
    result_type_version VARCHAR,
    payload VARCHAR,
    observed_at TIMESTAMP,
    ingest_batch_id VARCHAR
)
"""

_INSERT_ROW = "INSERT INTO result VALUES (" + ", ".join(
    "?" for _ in _RESULT_COLUMN_ORDER
) + ")"


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
        entity_hash: str,
        result_type: str,
        source: str,
        result_type_version: str,
        payload: dict,
        observed_at: str,
        ingest_batch_id: str,
        # Defaults so this file's own hand-built rows (the ones not coming
        # from the shared parity fixture) stay terse while the table's
        # declared column set grows. "protein" is the type every such row
        # used before entity_type joined the slot key; the DEFAULT parameter
        # set is likewise what every pre-parameter-set row means -- a run
        # with nothing set on top of the tool version's defaults. Tests
        # exercising the parameter-set slot key pass it explicitly.
        entity_type: str = "protein",
        parameter_set_hash: str = DEFAULT_PARAMETER_SET_HASH,
    ) -> None:
        # Bind the hex entity_hash as a parameter -- never a literal.
        values = {
            "entity_hash": entity_hash,
            "entity_type": entity_type,
            "result_type": result_type,
            "source": source,
            "parameter_set_hash": parameter_set_hash,
            "result_type_version": result_type_version,
            "payload": json.dumps(payload),
            "observed_at": observed_at,
            "ingest_batch_id": ingest_batch_id,
        }
        self.con.execute(
            _INSERT_ROW, [values[name] for name in _RESULT_COLUMN_ORDER]
        )

    def current_state(
        self,
        *,
        sources: list[str] | None = None,
        entity_types: list[str] | None = None,
        parameter_set_hashes: list[str] | None = None,
    ) -> list[dict]:
        sql = current_state_sql(
            _TABLE_FQN,
            sources=sources,
            entity_types=entity_types,
            parameter_set_hashes=parameter_set_hashes,
        )
        rows = self.con.execute(_for_duckdb(sql)).fetchall()
        return [
            dict(zip(_RESULT_COLUMN_ORDER, row, strict=True)) for row in rows
        ]


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

    ``_standardize_protein`` and ``_standardize_gene`` are the same
    standardizer (a bare ``_clean_sequence_letters``), so a sequence over
    the alphabet {A,C,G,T,N} produces a byte-identical ``entity_hash``
    whether submitted as a protein or as gene DNA. A protein row and a
    gene row that share an entity_hash, result_type and source must
    therefore occupy TWO distinct slots -- both must survive as current,
    never collapsed to one by a window function partitioned on
    entity_hash/result_type/source alone.
    """

    def test_protein_and_gene_sharing_hash_both_survive(self, fixture):
        entity_hash = fixture_entity_hash("shared-hash")
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
            entity_type="gene",
            payload={"kind": "gene"},
            **shared_kwargs,
        )
        rows = {
            row["entity_type"]: row
            for row in fixture.current_state()
            if row["entity_hash"] == entity_hash
        }
        assert set(rows) == {"protein", "gene"}
        assert json.loads(rows["protein"]["payload"]) == {"kind": "protein"}
        assert json.loads(rows["gene"]["payload"]) == {"kind": "gene"}

    def test_entity_types_filter_prunes_to_requested_types_only(self, fixture):
        entity_hash = fixture_entity_hash("shared-hash")
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
            entity_type="gene",
            payload={"kind": "gene"},
            **shared_kwargs,
        )
        rows = [
            row
            for row in fixture.current_state(entity_types=["protein"])
            if row["entity_hash"] == entity_hash
        ]
        assert {row["entity_type"] for row in rows} == {"protein"}

    def test_entity_types_and_sources_filters_combine_with_and(self, fixture):
        entity_hash = fixture_entity_hash("shared-hash")
        base = dict(
            entity_hash=entity_hash,
            result_type="annotation",
            result_type_version="v1",
            observed_at="2026-01-01 00:00:00",
        )
        # (protein, sourceA), (protein, sourceB), (gene, sourceA)
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
            entity_type="gene",
            source="parity-check/entity_type_and_source/toolA",
            payload={"kind": "gene_a"},
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
        entity_hash = fixture_entity_hash("shared-hash")
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
        entity_hash = fixture_entity_hash("shared-hash")
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


class TestParameterSetHashInSlotKey:
    """Property 9: ``parameter_set_hash`` is part of the slot key.

    Two parameter sets run through ONE tool version at one
    ``(entity_hash, entity_type, result_type, source)`` are two distinct
    current results, both current at once. ``source`` carries only
    ``<tool>/<version>``, so before the column joined the slot key the
    parameterised run would have superseded the default one -- silently
    destroying the default answer and serving a parameterised one to every
    caller that never asked for it.

    Rows come from the shared parity fixture, the same module the in-pod
    OP3 check appends, so the two cannot drift.
    """

    def test_two_parameter_sets_for_one_tool_version_are_both_current(
        self, fixture
    ):
        case = PARAMETER_SET_FORKS_SLOT
        for row in case.rows:
            fixture.insert(**row)
        rows = {
            row["parameter_set_hash"]: row
            for row in fixture.current_state()
            if row["entity_hash"] == case.entity_hash
        }
        default_hash, evalue_hash = case.parameter_set_hashes
        assert set(rows) == {default_hash, evalue_hash}
        for hash_value, expected in PARAMETER_SET_EXPECTED_PAYLOADS.items():
            assert json.loads(rows[hash_value]["payload"]) == expected
        # The two rows really do share every other slot-key column, so this
        # is a slot-key test and not an incidental one.
        assert len({row["source"] for row in rows.values()}) == 1

    def test_the_default_row_survives_a_later_parameterised_append(
        self, fixture
    ):
        """The regression this column exists to prevent, stated directly.

        The parameterised row is the NEWER of the two, so under the old
        4-tuple slot key it would have won the shared slot and the default
        row would be absent from current state entirely.
        """
        case = PARAMETER_SET_FORKS_SLOT
        for row in case.rows:
            fixture.insert(**row)
        rows = [
            row
            for row in fixture.current_state()
            if row["entity_hash"] == case.entity_hash
        ]
        assert len(rows) == 2
        default_rows = [
            row
            for row in rows
            if row["parameter_set_hash"] == DEFAULT_PARAMETER_SET_HASH
        ]
        assert len(default_rows) == 1
        assert json.loads(default_rows[0]["payload"]) == {
            "ns": {"k": "default_params"}
        }

    def test_parameter_set_hashes_filter_returns_exactly_its_own_row(
        self, fixture
    ):
        case = PARAMETER_SET_FORKS_SLOT
        for row in case.rows:
            fixture.insert(**row)
        _default_hash, evalue_hash = case.parameter_set_hashes
        rows = [
            row
            for row in fixture.current_state(
                parameter_set_hashes=[evalue_hash]
            )
            if row["entity_hash"] == case.entity_hash
        ]
        assert len(rows) == 1
        assert rows[0]["parameter_set_hash"] == evalue_hash
        assert json.loads(rows[0]["payload"]) == (
            PARAMETER_SET_EXPECTED_PAYLOADS[evalue_hash]
        )

    def test_filtering_by_the_default_hash_returns_the_default_row(
        self, fixture
    ):
        case = PARAMETER_SET_FORKS_SLOT
        for row in case.rows:
            fixture.insert(**row)
        rows = [
            row
            for row in fixture.current_state(
                parameter_set_hashes=[DEFAULT_PARAMETER_SET_HASH]
            )
            if row["entity_hash"] == case.entity_hash
        ]
        assert len(rows) == 1
        assert json.loads(rows[0]["payload"]) == {"ns": {"k": "default_params"}}

    def test_empty_parameter_set_hashes_matches_nothing_on_execution(
        self, fixture
    ):
        case = PARAMETER_SET_FORKS_SLOT
        for row in case.rows:
            fixture.insert(**row)
        assert fixture.current_state(parameter_set_hashes=[]) == []

    def test_newer_row_still_wins_within_one_parameter_set(self, fixture):
        """Supersession is unchanged INSIDE a parameter set.

        Widening the slot key must not stop a re-run of the SAME parameter
        set from superseding its predecessor -- otherwise every re-run
        would accumulate as a separate current row.
        """
        entity = fixture_entity_hash("param-set-supersede")
        source = "parity-check/param_set_supersede/toolA"
        for observed_at, batch, marker in (
            ("2026-03-01 00:00:00", "01HPSUPERSEDE000000000AA", "old"),
            ("2026-03-02 00:00:00", "01HPSUPERSEDE000000000AB", "new"),
        ):
            fixture.insert(
                entity_hash=entity,
                result_type="annotation",
                source=source,
                parameter_set_hash=PARAMETER_SET_EVALUE_HASH,
                result_type_version="v1",
                payload={"ns": {"k": marker}},
                observed_at=observed_at,
                ingest_batch_id=batch,
            )
        rows = [
            row
            for row in fixture.current_state()
            if row["entity_hash"] == entity
        ]
        assert len(rows) == 1
        assert json.loads(rows[0]["payload"]) == {"ns": {"k": "new"}}

    def test_result_type_version_alone_still_never_forks_a_slot(self, fixture):
        """Two rows on one parameter set differing ONLY in
        ``result_type_version`` still collapse to one current row.

        ``result_type_version`` stayed OUT of the slot key when
        ``parameter_set_hash`` joined it; this asserts the widened key did
        not drag it in.
        """
        entity = fixture_entity_hash("param-set-version")
        source = "parity-check/param_set_version/toolA"
        for version, observed_at, batch in (
            ("v1", "2026-04-01 00:00:00", "01HPSVERSION0000000000AA"),
            ("v2", "2026-04-02 00:00:00", "01HPSVERSION0000000000AB"),
        ):
            fixture.insert(
                entity_hash=entity,
                result_type="annotation",
                source=source,
                parameter_set_hash=PARAMETER_SET_EVALUE_HASH,
                result_type_version=version,
                payload={"ns": {"v": version}},
                observed_at=observed_at,
                ingest_batch_id=batch,
            )
        rows = [
            row
            for row in fixture.current_state()
            if row["entity_hash"] == entity
        ]
        assert len(rows) == 1
        assert rows[0]["result_type_version"] == "v2"

    def test_one_parameter_set_per_slot_is_unaffected_by_another_entity(
        self, fixture
    ):
        """Two parameter sets stay scoped to their own entity.

        A parameter_set_hashes filter is a row filter, not a slot filter,
        so this guards against it accidentally pulling in another entity's
        row for the same hash.
        """
        case = PARAMETER_SET_FORKS_SLOT
        for row in case.rows:
            fixture.insert(**row)
        other_entity = fixture_entity_hash("param-set-other-entity")
        fixture.insert(
            entity_hash=other_entity,
            result_type="annotation",
            source=case.rows[0]["source"],
            parameter_set_hash=PARAMETER_SET_EVALUE_HASH,
            result_type_version="v1",
            payload={"ns": {"k": "other_entity"}},
            observed_at="2026-05-01 00:00:00",
            ingest_batch_id="01HPSOTHER00000000000AA",
        )
        rows = fixture.current_state(
            parameter_set_hashes=[PARAMETER_SET_EVALUE_HASH]
        )
        by_entity = {row["entity_hash"]: row for row in rows}
        assert set(by_entity) == {case.entity_hash, other_entity}
        assert all(
            row["parameter_set_hash"] == PARAMETER_SET_EVALUE_HASH
            for row in rows
        )

    def test_the_fixture_hashes_come_from_the_identity_rule(self):
        """Not hardcoded hex: the fixture's hashes are what
        ``parameter_set_hash`` actually returns for those sets.
        """
        assert parameter_set_hash({}) == DEFAULT_PARAMETER_SET_HASH
        assert parameter_set_hash({"evalue": "1e-10"}) == (
            PARAMETER_SET_EVALUE_HASH
        )


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

    def test_partition_by_names_all_five_slot_key_columns_in_order(self):
        sql = current_state_sql("ns.result")
        assert (
            "PARTITION BY entity_hash, entity_type, result_type, source, "
            "parameter_set_hash" in sql
        )

    def test_result_type_version_is_not_in_the_window_key(self):
        """Still deliberately OUT of the slot key, even now the key grew.

        If it were in, bumping a result-type schema would fork every slot
        in the corpus and nothing would ever supersede its predecessor
        again -- see the module docstring.
        """
        sql = current_state_sql("ns.result")
        partition_line = next(
            line for line in sql.splitlines() if "PARTITION BY" in line
        )
        assert "result_type_version" not in partition_line

    def test_parameter_set_hashes_filter_renders_as_where_in(self):
        sql = current_state_sql(
            "ns.result", parameter_set_hashes=[DEFAULT_PARAMETER_SET_HASH]
        )
        assert (
            f"WHERE parameter_set_hash IN ('{DEFAULT_PARAMETER_SET_HASH}')" in sql
        )

    def test_empty_parameter_set_hashes_list_filters_out_everything(self):
        sql = current_state_sql("ns.result", parameter_set_hashes=[])
        assert "WHERE 1 = 0" in sql
        assert "parameter_set_hash IN" not in sql

    def test_no_parameter_set_hashes_filter_when_none(self):
        sql = current_state_sql("ns.result")
        assert "parameter_set_hash IN" not in sql
        assert "1 = 0" not in sql

    def test_parameter_set_hashes_combines_with_the_other_filters_by_and(self):
        sql = current_state_sql(
            "ns.result",
            sources=["toolA/1"],
            entity_types=["protein"],
            parameter_set_hashes=[DEFAULT_PARAMETER_SET_HASH],
        )
        assert (
            "WHERE source IN ('toolA/1') AND entity_type IN ('protein') AND "
            f"parameter_set_hash IN ('{DEFAULT_PARAMETER_SET_HASH}')" in sql
        )
        assert " OR " not in sql

    @pytest.mark.parametrize(
        "bad_value",
        [
            DEFAULT_PARAMETER_SET_HASH.upper(),  # uppercase matches no row
            "a" * 63,  # too short
            "a" * 65,  # too long
            "g" * 64,  # not hex
            "",
            "' OR 1=1 --",  # the injection shape literal embedding exposes
            DEFAULT_PARAMETER_SET_HASH + " ",
        ],
    )
    def test_non_hex_parameter_set_hash_raises_value_error(self, bad_value):
        with pytest.raises(ValueError, match="parameter_set_hashes"):
            current_state_sql("ns.result", parameter_set_hashes=[bad_value])

    def test_bad_hash_raises_even_alongside_a_match_nothing_filter(self):
        """A malformed digest is reported, not swallowed by ``sources=[]``.

        Were validation ordered after the match-nothing short-circuit, a
        caller passing both would get a silent ``WHERE 1 = 0`` query and
        never learn its digest was wrong.
        """
        with pytest.raises(ValueError, match="parameter_set_hashes"):
            current_state_sql(
                "ns.result", sources=[], parameter_set_hashes=["NOTAHASH"]
            )

    def test_a_valid_hash_is_never_rewritten_in_the_emitted_sql(self):
        sql = current_state_sql(
            "ns.result", parameter_set_hashes=[PARAMETER_SET_EVALUE_HASH]
        )
        assert PARAMETER_SET_EVALUE_HASH in sql

    def test_entity_types_filter_renders_as_where_in(self):
        sql = current_state_sql("ns.result", entity_types=["protein", "gene"])
        assert "WHERE entity_type IN ('protein', 'gene')" in sql

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
