"""``council_sink`` -- the row-level read/write seam over the council namespace.

:class:`SqliteCouncilSink` is tested against a REAL SQLite file whose nine
tables are created from ``council_schema``'s own ``schema_sql``, so the DDL,
the appends and the reads are all real. :class:`BerdlCouncilSink` is tested
only for what can be tested off-pod: that it refuses an unknown column before
touching a transport, and that its row encoding matches the local sink's.

NOT A CLAIM ABOUT ICEBERG. SQLite has permissive type affinities, no
partition specs and no snapshot history. Nothing here establishes that the
two sinks agree on a live pod -- that is operator-owned acceptance
(KBUtilLib#91) and is not asserted anywhere in this repo.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from kbutillib.domains.kbase.berdl.council_schema import (
    ARCHIVED_TABLES,
    COUNCIL_NAMESPACE,
    column_names,
)
from kbutillib.domains.kbase.berdl.council_sink import (
    BerdlCouncilSink,
    SqliteCouncilSink,
    UnknownColumnError,
    encode_row,
    encode_value,
    open_sink,
)


@pytest.fixture
def sink(tmp_path):
    opened = SqliteCouncilSink(tmp_path / "council.sqlite")
    opened.bootstrap()
    try:
        yield opened
    finally:
        opened.close()


def _identity(**extra):
    row = {
        "store_id": "poplar/council-1",
        "snapshot_id": "a" * 64,
        "snapshot_seq": 1,
        "store_host": "poplar",
        "loaded_at": "2026-10-25T00:00:00+00:00",
        "load_batch_id": "01JBATCH0000000000000000001",
    }
    row.update(extra)
    return row


class TestTheDdlIsReal:
    def test_all_nine_tables_are_created_with_the_declared_columns(self, sink):
        for table in ARCHIVED_TABLES:
            assert sink.table_exists(table), table
            assert sink.catalog_columns(table) == column_names(table), table

    def test_bootstrap_is_idempotent(self, sink):
        sink.bootstrap()
        sink.bootstrap()
        for table in ARCHIVED_TABLES:
            assert sink.catalog_columns(table) == column_names(table), table

    def test_no_primary_key_is_declared_matching_the_namespace(self, sink):
        """The lakehouse enforces no key either; a stricter sink would pass
        tests the real target would fail."""
        rows = sink._connection.execute(
            f'PRAGMA table_info("{sink.physical_name("jobs")}")'
        ).fetchall()
        assert all(row["pk"] == 0 for row in rows)


class TestAppendAndRead:
    def test_a_row_round_trips(self, sink):
        sink.append_rows("workers", [_identity(worker_id="w1", host="poplar")])
        rows = sink.select_rows("workers")
        assert len(rows) == 1
        assert rows[0]["worker_id"] == "w1"
        # Undeclared-by-the-caller columns land as NULL, not as absent keys.
        assert rows[0]["version"] is None

    def test_appending_nothing_writes_nothing(self, sink):
        assert sink.append_rows("workers", [])["rows"] == 0
        assert sink.count_rows("workers") == 0

    def test_appends_never_replace(self, sink):
        sink.append_rows("workers", [_identity(worker_id="w1")])
        sink.append_rows("workers", [_identity(worker_id="w1")])
        assert sink.count_rows("workers") == 2

    def test_a_filter_binds_its_value_rather_than_formatting_it(self, sink):
        """A value that would break out of quoted SQL text must not."""
        sink.append_rows("workers", [_identity(worker_id="w' OR 1=1 --")])
        sink.append_rows("workers", [_identity(worker_id="w2")])
        assert sink.count_rows("workers", where={"worker_id": "w' OR 1=1 --"}) == 1

    def test_an_in_filter_matches_a_set(self, sink):
        for name in ("w1", "w2", "w3"):
            sink.append_rows("workers", [_identity(worker_id=name)])
        rows = sink.select_rows("workers", where={"worker_id": ["w1", "w3"]})
        assert sorted(r["worker_id"] for r in rows) == ["w1", "w3"]

    def test_an_empty_in_filter_matches_nothing(self, sink):
        sink.append_rows("workers", [_identity(worker_id="w1")])
        assert sink.select_rows("workers", where={"worker_id": []}) == []

    def test_a_none_filter_means_is_null(self, sink):
        sink.append_rows("workers", [_identity(worker_id="w1", version=None)])
        sink.append_rows("workers", [_identity(worker_id="w2", version="1.2")])
        rows = sink.select_rows("workers", where={"version": None})
        assert [r["worker_id"] for r in rows] == ["w1"]

    def test_order_by_is_honoured(self, sink):
        for seq in (3, 1, 2):
            sink.append_rows("workers", [_identity(snapshot_seq=seq, worker_id=f"w{seq}")])
        rows = sink.select_rows("workers", order_by=("snapshot_seq",))
        assert [r["snapshot_seq"] for r in rows] == [1, 2, 3]

    def test_a_null_stays_null_and_never_becomes_the_string_null(self, sink):
        """The reply_parsed trap: sa.JSON with none_as_null=False stores
        Python None as the TEXT 'null', so IS NOT NULL is true for every
        row. Nothing in this seam may reproduce that."""
        sink.append_rows(
            "calls", [_identity(call_id="c1", reply_parsed=None, reply_text=None)]
        )
        row = sink.select_rows("calls")[0]
        assert row["reply_parsed"] is None
        assert row["reply_parsed"] != "null"
        assert sink.count_rows("calls", where={"reply_parsed": None}) == 1


class TestRefusals:
    def test_an_unknown_column_in_a_row_is_refused(self, sink):
        with pytest.raises(UnknownColumnError) as excinfo:
            sink.append_rows("workers", [_identity(worker_id="w1", pid=42)])
        assert "pid" in str(excinfo.value)
        assert sink.count_rows("workers") == 0

    def test_an_unknown_column_in_a_filter_is_refused(self, sink):
        with pytest.raises(UnknownColumnError):
            sink.select_rows("workers", where={"pid": 42})

    def test_an_unknown_requested_column_is_refused(self, sink):
        with pytest.raises(UnknownColumnError):
            sink.select_rows("workers", columns=("pid",))

    def test_an_unknown_order_column_is_refused(self, sink):
        with pytest.raises(UnknownColumnError):
            sink.select_rows("workers", order_by=("pid",))

    def test_a_table_that_deliberately_does_not_archive_is_refused(self, sink):
        with pytest.raises(ValueError) as excinfo:
            sink.select_rows("schema_version")
        assert "NOT_ARCHIVED" in str(excinfo.value)


class TestValueEncoding:
    def test_a_datetime_becomes_iso_8601(self):
        stamp = datetime(2026, 10, 25, 12, 30, tzinfo=timezone.utc)
        assert encode_value(stamp) == "2026-10-25T12:30:00+00:00"

    def test_a_mapping_is_json_with_sorted_keys(self):
        """One row's bytes must not depend on dict iteration order -- the
        on-disk and in-lakehouse copies of a bundle are compared by value."""
        assert encode_value({"b": 1, "a": 2}) == encode_value({"a": 2, "b": 1})
        assert json.loads(encode_value({"b": 1, "a": 2})) == {"a": 2, "b": 1}

    def test_none_passes_through_untouched(self):
        assert encode_value(None) is None

    def test_a_bool_stays_a_bool(self):
        assert encode_value(True) is True
        assert encode_value(False) is False

    def test_encode_row_fills_every_declared_column_in_ddl_order(self):
        row = encode_row("workers", _identity(worker_id="w1"))
        assert tuple(row) == column_names("workers")


class TestThePodSink:
    """Only what is testable off-pod. Nothing here reaches a transport."""

    class _Refusing:
        def load(self, **kwargs):  # pragma: no cover - must never be reached
            raise AssertionError("load() must not be called")

        def query(self, sql, params=None):  # pragma: no cover
            raise AssertionError("query() must not be called")

    def test_an_unknown_column_is_refused_BEFORE_the_transport(self):
        sink = BerdlCouncilSink(self._Refusing())
        with pytest.raises(UnknownColumnError):
            sink.append_rows("workers", [_identity(worker_id="w1", pid=1)])

    def test_the_default_namespace_is_the_council_namespace(self):
        assert BerdlCouncilSink(self._Refusing()).namespace == COUNCIL_NAMESPACE

    def test_the_physical_name_is_namespace_qualified(self):
        sink = BerdlCouncilSink(self._Refusing())
        assert sink.physical_name("jobs") == f"{COUNCIL_NAMESPACE}.jobs"

    def test_an_append_requests_mode_append_and_never_overwrite(self):
        class _Recording:
            def __init__(self):
                self.calls = []

            def load(self, **kwargs):
                self.calls.append(kwargs)
                return {"ok": True}

        pytest.importorskip("pandas", reason="the pod append path builds a DataFrame")
        capability = _Recording()
        sink = BerdlCouncilSink(capability)
        sink.append_rows("workers", [_identity(worker_id="w1")])
        tables = capability.calls[0]["tables"]
        assert [t["mode"] for t in tables] == ["append"]
        assert all(t["mode"] != "overwrite" for t in tables)
        frame = capability.calls[0]["dataframes"]["workers"]
        assert list(frame.columns) == list(column_names("workers"))

    def test_a_filter_is_bound_as_a_parameter_never_formatted(self):
        class _Capturing:
            def __init__(self):
                self.sql = None
                self.params = None

            def query(self, sql, params=None):
                self.sql, self.params = sql, params
                return []

        capability = _Capturing()
        BerdlCouncilSink(capability).select_rows("workers", where={"worker_id": "w'1"})
        assert "w'1" not in capability.sql
        assert capability.params == ["w'1"]


class TestOpenSink:
    def test_a_path_gives_a_bootstrapped_local_sink(self, tmp_path):
        sink = open_sink(tmp_path / "council.sqlite")
        try:
            assert isinstance(sink, SqliteCouncilSink)
            for table in ARCHIVED_TABLES:
                assert sink.table_exists(table), table
        finally:
            sink.close()


class TestBothSinksBootstrapTheSameSet:
    """A table that exists locally and not in the pod is the worst outcome.

    ``publication_bundle`` is registered outside ``council_schema`` (see
    ``register_table``), so a pod sink that defaulted to ``council_schema``'s
    nine would report a bootstrapped namespace and then fail on the first
    bundle write.
    """

    class _Recording:
        def __init__(self):
            self.calls = []

        def table_exists(self, name, *, namespace):
            return False

        def table_partition_spec(self, name, *, namespace):
            return []

        def load(self, **kwargs):
            self.calls.append(kwargs)
            return {"created_or_appended": [t["name"] for t in kwargs["tables"]]}

    def test_the_pod_sink_bootstraps_every_registered_table_too(self):
        from kbutillib.domains.genome.ontology_mapping.council_bundle import (
            PUBLICATION_BUNDLE_TABLE,
        )

        capability = self._Recording()
        BerdlCouncilSink(capability).bootstrap()
        requested = {t["name"] for call in capability.calls for t in call["tables"]}
        assert set(ARCHIVED_TABLES) <= requested
        assert PUBLICATION_BUNDLE_TABLE in requested

    def test_the_local_sink_creates_the_same_set(self, sink):
        from kbutillib.domains.genome.ontology_mapping.council_bundle import (
            PUBLICATION_BUNDLE_TABLE,
        )

        for table in (*ARCHIVED_TABLES, PUBLICATION_BUNDLE_TABLE):
            assert sink.table_exists(table), table


class TestRegisterTable:
    def test_a_name_that_collides_with_council_schema_is_refused(self):
        from kbutillib.domains.kbase.berdl.council_sink import register_table

        with pytest.raises(Exception) as excinfo:
            register_table("jobs", (("x", "STRING"),))
        assert "council_schema" in str(excinfo.value)

    def test_re_registering_the_SAME_columns_is_idempotent(self):
        from kbutillib.domains.kbase.berdl.council_sink import register_table

        from kbutillib.domains.kbase.berdl.council_sink import (
            column_names as sink_column_names,
        )

        register_table("t_sink_probe", (("a", "STRING"), ("b", "INT")))
        register_table("t_sink_probe", (("a", "STRING"), ("b", "INT")))
        # The SINK's resolver, which answers for both declaration sites;
        # council_schema's own still refuses an unknown name, as it should.
        assert sink_column_names("t_sink_probe") == ("a", "b")
        with pytest.raises(ValueError):
            column_names("t_sink_probe")

    def test_re_registering_DIFFERENT_columns_is_refused(self):
        """Two column lists for one physical table is the drift one
        declaration site prevents."""
        from kbutillib.domains.kbase.berdl.council_sink import register_table

        register_table("t_sink_conflict", (("a", "STRING"),))
        with pytest.raises(Exception) as excinfo:
            register_table("t_sink_conflict", (("a", "INT"),))
        assert "different column list" in str(excinfo.value)

    def test_a_parenthesised_type_is_refused(self):
        """No config value here may contain a parenthesis -- it is the guard
        against an Iceberg partition-transform expression leaking in."""
        from kbutillib.domains.kbase.berdl.council_sink import register_table

        with pytest.raises(Exception) as excinfo:
            register_table("t_sink_paren", (("a", "DECIMAL(10,2)"),))
        assert "parenthesis" in str(excinfo.value)
