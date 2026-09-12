"""Unit tests for :mod:`kbutillib.domains.kbase.berdl.clearinghouse_bootstrap_adapter`.

Pure logic, no network, no live BERDL pod, no real Spark: every scenario
here drives :class:`ClearinghouseBootstrapCapability` against hand-built
fakes (``_FakeTransport``, ``_FakeUnderlyingCapability`` below), and the
SQL emitters/parsers directly against plain tuples standing in for
collected Spark rows -- never a real ``BerdlCapability`` or a live
cluster. See ``tests/berdl/test_clearinghouse_bootstrap.py`` for the
sibling test module covering ``bootstrap()`` itself against a fake
capability satisfying the same three-method contract this adapter is
built to satisfy for real.
"""

from __future__ import annotations

import pytest

from kbutillib.domains.kbase.berdl.capability import BerdlCapability
from kbutillib.domains.kbase.berdl.clearinghouse_bootstrap_adapter import (
    ClearinghouseBootstrapCapability,
    PartitionSpecUnparseableError,
    _describe_table_extended_sql,
    _parse_describe_table_extended_partition_spec,
    _parse_partition_spec,
    _parse_show_create_table_partition_spec,
    _show_create_table_sql,
)
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    BootstrapIndeterminateStateError,
    bootstrap,
)

_NAMESPACE = "clearinghouse"


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _FakeTransport:
    """Stands in for ``InPodTransport``: only ``spark_session`` and
    ``table_exists`` -- the two members the adapter actually calls.
    """

    def __init__(self, spark=None, *, exists=True, exists_error=None):
        self._spark = spark if spark is not None else object()
        self._exists = exists
        self._exists_error = exists_error
        self.spark_session_calls = 0
        self.table_exists_calls: list[tuple] = []

    def spark_session(self):
        self.spark_session_calls += 1
        return self._spark

    def table_exists(self, spark, name, *, namespace):
        self.table_exists_calls.append((spark, name, namespace))
        if self._exists_error is not None:
            raise self._exists_error
        return self._exists


class _FakeUnderlyingCapability:
    """Stands in for ``BerdlCapability``: only ``load`` and ``query``."""

    def __init__(self, *, query_result=None, query_error=None):
        self.load_calls: list[dict] = []
        self.query_calls: list[tuple] = []
        self._query_result = query_result if query_result is not None else []
        self._query_error = query_error

    def load(self, **kwargs):
        self.load_calls.append(kwargs)
        return {"ingest_result": "ok", "tables": kwargs.get("tables")}

    def query(self, sql, **kwargs):
        self.query_calls.append((sql, kwargs))
        if self._query_error is not None:
            raise self._query_error
        return self._query_result


def _describe_extended_rows(*part_pairs: tuple[str, str]) -> list[tuple]:
    """Build fake ``DESCRIBE TABLE EXTENDED`` rows for the given partition
    columns, in order, wrapped with the headers a real result carries.
    """
    rows = [("entity_hash", "BINARY", None), ("entity_type", "STRING", None)]
    if part_pairs:
        rows.append(("# Partitioning", "", ""))
        for i, (_, col) in enumerate(part_pairs):
            rows.append((f"Part {i}", col, None))
    rows.append(("# Detailed Table Information", "", ""))
    rows.append(("Name", f"{_NAMESPACE}.some_table", None))
    return rows


# --------------------------------------------------------------------------
# 1 + 2: the three-method contract
# --------------------------------------------------------------------------


class TestAdapterSatisfiesBootstrapContract:
    def test_bootstrap_accepts_adapter_without_attributeerror(self):
        transport = _FakeTransport(exists=False)
        underlying = _FakeUnderlyingCapability()
        adapter = ClearinghouseBootstrapCapability(underlying, transport=transport)

        result = bootstrap(adapter, namespace=_NAMESPACE)

        assert result["dry_run"] is False
        assert len(underlying.load_calls) == 1
        assert all(t["action"] == "create" for t in result["tables"])

    def test_bare_berdl_capability_still_fails_the_bootstrap_contract(self):
        """A bare ``BerdlCapability`` has no ``table_exists``/
        ``table_partition_spec`` -- attempting ``capability.table_exists``
        inside ``bootstrap()``'s existence check raises ``AttributeError``,
        which ``bootstrap()``'s own ``except Exception`` wraps into
        ``BootstrapIndeterminateStateError`` (never treating the
        indeterminate lookup as "does not exist"). This is exactly the gap
        the adapter exists to close -- documented here by test, not prose.
        """
        cap = BerdlCapability()

        with pytest.raises(BootstrapIndeterminateStateError) as excinfo:
            bootstrap(cap, namespace=_NAMESPACE)

        assert isinstance(excinfo.value.__cause__, AttributeError)
        assert "table_exists" in str(excinfo.value.__cause__)


# --------------------------------------------------------------------------
# 3: session identity between table_exists and load
# --------------------------------------------------------------------------


class TestSessionIdentity:
    def test_table_exists_and_load_share_the_same_spark_session(self):
        session = object()
        transport = _FakeTransport(session, exists=True)
        underlying = _FakeUnderlyingCapability()
        adapter = ClearinghouseBootstrapCapability(underlying, transport=transport)

        adapter.table_exists("entity", namespace=_NAMESPACE)
        adapter.load(dataset="clearinghouse", tables=[], namespace=_NAMESPACE)

        assert transport.table_exists_calls[0][0] is session
        assert underlying.load_calls[0]["spark"] is session
        # Only resolved once, not once per call.
        assert transport.spark_session_calls == 1

    def test_explicitly_supplied_spark_is_used_and_never_re_resolved(self):
        session = object()
        transport = _FakeTransport(exists=True)
        underlying = _FakeUnderlyingCapability()
        adapter = ClearinghouseBootstrapCapability(
            underlying, spark=session, transport=transport
        )

        adapter.table_exists("entity", namespace=_NAMESPACE)
        adapter.load(dataset="clearinghouse", tables=[], namespace=_NAMESPACE)

        assert transport.table_exists_calls[0][0] is session
        assert underlying.load_calls[0]["spark"] is session
        assert transport.spark_session_calls == 0

    def test_load_ignores_a_caller_supplied_spark_kwarg(self):
        """The adapter owns session identity end-to-end -- a caller
        passing its own ``spark=`` into ``load()`` must not be able to
        desynchronize it from the session ``table_exists`` probes with.
        """
        session = object()
        other_session = object()
        transport = _FakeTransport(session, exists=True)
        underlying = _FakeUnderlyingCapability()
        adapter = ClearinghouseBootstrapCapability(underlying, transport=transport)

        adapter.table_exists("entity", namespace=_NAMESPACE)
        adapter.load(
            dataset="clearinghouse",
            tables=[],
            namespace=_NAMESPACE,
            spark=other_session,
        )

        assert underlying.load_calls[0]["spark"] is session
        assert underlying.load_calls[0]["spark"] is not other_session


# --------------------------------------------------------------------------
# 4: SQL emitter string shapes
# --------------------------------------------------------------------------


class TestSqlEmitters:
    def test_describe_table_extended_sql(self):
        assert _describe_table_extended_sql("entity", "clearinghouse") == (
            "DESCRIBE TABLE EXTENDED `clearinghouse`.`entity`"
        )

    def test_show_create_table_sql(self):
        assert _show_create_table_sql("result", "clearinghouse") == (
            "SHOW CREATE TABLE `clearinghouse`.`result`"
        )

    def test_emitters_backtick_quote_namespace_and_name_separately(self):
        sql = _describe_table_extended_sql("my.table", "my.ns")
        assert "`my.ns`.`my.table`" in sql


# --------------------------------------------------------------------------
# 5: parsers for both recognised shapes, order asserted
# --------------------------------------------------------------------------


class TestDescribeExtendedParser:
    def test_two_column_spec_is_returned_in_declared_order(self):
        rows = _describe_extended_rows(("Part 0", "source"), ("Part 1", "entity_type"))
        result = _parse_describe_table_extended_partition_spec(
            rows, name="result", namespace=_NAMESPACE
        )
        assert result == ["source", "entity_type"]
        # Not just set-equal -- a flipped order must fail this assertion.
        assert result != ["entity_type", "source"]

    def test_single_column_spec(self):
        rows = _describe_extended_rows(("Part 0", "entity_type"))
        result = _parse_describe_table_extended_partition_spec(
            rows, name="entity", namespace=_NAMESPACE
        )
        assert result == ["entity_type"]

    def test_positively_unpartitioned_table_returns_empty_list(self):
        rows = _describe_extended_rows()  # no Part rows, no header at all
        result = _parse_describe_table_extended_partition_spec(
            rows, name="canonical_content", namespace=_NAMESPACE
        )
        assert result == []
        assert result is not None

    def test_unrecognised_shape_returns_none_not_empty_list(self):
        """No '# Detailed Table Information' header at all -- this is not
        a DESCRIBE TABLE EXTENDED result, so the parser must signal 'try
        the next shape' (None), never 'unpartitioned' ([]).
        """
        rows = [("some", "garbage", "row")]
        result = _parse_describe_table_extended_partition_spec(
            rows, name="entity", namespace=_NAMESPACE
        )
        assert result is None

    def test_transform_expression_raises(self):
        rows = _describe_extended_rows(("Part 0", "bucket(256, entity_hash)"))
        with pytest.raises(PartitionSpecUnparseableError, match="transform"):
            _parse_describe_table_extended_partition_spec(
                rows, name="entity", namespace=_NAMESPACE
            )


class TestShowCreateTableParser:
    def test_two_column_spec_is_returned_in_declared_order(self):
        ddl = (
            "CREATE TABLE clearinghouse.result (entity_hash BINARY, "
            "source STRING, entity_type STRING) USING iceberg "
            "PARTITIONED BY (source, entity_type)"
        )
        result = _parse_show_create_table_partition_spec(
            [(ddl,)], name="result", namespace=_NAMESPACE
        )
        assert result == ["source", "entity_type"]
        assert result != ["entity_type", "source"]

    def test_single_column_spec_with_backticks(self):
        ddl = (
            "CREATE TABLE clearinghouse.entity (entity_type STRING) "
            "USING iceberg PARTITIONED BY (`entity_type`)"
        )
        result = _parse_show_create_table_partition_spec(
            [(ddl,)], name="entity", namespace=_NAMESPACE
        )
        assert result == ["entity_type"]

    def test_positively_unpartitioned_table_returns_empty_list(self):
        ddl = "CREATE TABLE clearinghouse.canonical_content (content STRING) USING iceberg"
        result = _parse_show_create_table_partition_spec(
            [(ddl,)], name="canonical_content", namespace=_NAMESPACE
        )
        assert result == []
        assert result is not None

    def test_unrecognised_shape_returns_none_not_empty_list(self):
        result = _parse_show_create_table_partition_spec(
            [("this is not any kind of DDL statement",)],
            name="entity",
            namespace=_NAMESPACE,
        )
        assert result is None

    def test_more_than_one_row_is_unrecognised(self):
        result = _parse_show_create_table_partition_spec(
            [("CREATE TABLE x ...",), ("extra row",)],
            name="entity",
            namespace=_NAMESPACE,
        )
        assert result is None

    def test_transform_expression_raises(self):
        ddl = (
            "CREATE TABLE clearinghouse.entity (entity_hash BINARY) "
            "USING iceberg PARTITIONED BY (bucket(256, entity_hash))"
        )
        with pytest.raises(PartitionSpecUnparseableError, match="transform"):
            _parse_show_create_table_partition_spec(
                [(ddl,)], name="entity", namespace=_NAMESPACE
            )

    def test_transform_expression_does_not_break_top_level_comma_split(self):
        """A transform's own internal comma (inside its parens) must not
        be treated as a top-level PARTITIONED BY separator -- this table
        has exactly one partition entry, which happens to be a transform,
        not two entries ('bucket(256' and 'entity_hash)').
        """
        ddl = (
            "CREATE TABLE clearinghouse.entity (entity_hash BINARY) "
            "USING iceberg PARTITIONED BY (bucket(256, entity_hash))"
        )
        with pytest.raises(PartitionSpecUnparseableError) as excinfo:
            _parse_show_create_table_partition_spec(
                [(ddl,)], name="entity", namespace=_NAMESPACE
            )
        assert "bucket(256, entity_hash)" in str(excinfo.value)


# --------------------------------------------------------------------------
# 6: unparseable output raises, never returns []
# --------------------------------------------------------------------------


class TestDispatchNeverFallsBackToEmpty:
    def test_unrecognised_output_raises_not_returns_empty_list(self):
        garbage_rows = [("nonsense", "output", "here")]
        with pytest.raises(PartitionSpecUnparseableError) as excinfo:
            _parse_partition_spec(garbage_rows, name="canonical_content", namespace=_NAMESPACE)
        # The canonical_content trap: an unparseable result must never be
        # silently treated as [] (which bootstrap() reads as "matches the
        # deliberately-unpartitioned config" and lets through).
        assert "canonical_content" in str(excinfo.value)

    def test_dispatch_prefers_describe_extended_when_recognised(self):
        rows = _describe_extended_rows(("Part 0", "entity_type"))
        result = _parse_partition_spec(rows, name="entity", namespace=_NAMESPACE)
        assert result == ["entity_type"]

    def test_dispatch_falls_through_to_show_create_when_not_describe_shaped(self):
        ddl = (
            "CREATE TABLE clearinghouse.result (source STRING, "
            "entity_type STRING) USING iceberg PARTITIONED BY (source, entity_type)"
        )
        result = _parse_partition_spec([(ddl,)], name="result", namespace=_NAMESPACE)
        assert result == ["source", "entity_type"]

    def test_dispatch_error_message_quotes_a_snippet_of_the_raw_output(self):
        garbage_rows = [("nonsense", "output")] * 50  # long enough to truncate
        with pytest.raises(PartitionSpecUnparseableError) as excinfo:
            _parse_partition_spec(garbage_rows, name="entity", namespace=_NAMESPACE)
        message = str(excinfo.value)
        assert "entity" in message
        assert "nonsense" in message


# --------------------------------------------------------------------------
# 7: transform expression raises (dispatch-level, both shapes covered above)
# --------------------------------------------------------------------------


class TestTransformRaisesThroughDispatch:
    def test_transform_in_describe_extended_shape_raises_via_dispatch(self):
        rows = _describe_extended_rows(("Part 0", "days(observed_at)"))
        with pytest.raises(PartitionSpecUnparseableError):
            _parse_partition_spec(rows, name="entity", namespace=_NAMESPACE)


# --------------------------------------------------------------------------
# 8: positively-unpartitioned table returns []
# --------------------------------------------------------------------------


class TestPositivelyUnpartitioned:
    def test_dispatch_returns_empty_list_for_unpartitioned_describe_shape(self):
        rows = _describe_extended_rows()
        result = _parse_partition_spec(rows, name="canonical_content", namespace=_NAMESPACE)
        assert result == []


# --------------------------------------------------------------------------
# 9: table_exists exceptions propagate, become BootstrapIndeterminateStateError
# --------------------------------------------------------------------------


class TestTableExistsExceptionPropagation:
    def test_adapter_table_exists_does_not_swallow_the_exception(self):
        transport = _FakeTransport(exists_error=RuntimeError("catalog lookup timed out"))
        underlying = _FakeUnderlyingCapability()
        adapter = ClearinghouseBootstrapCapability(underlying, transport=transport)

        with pytest.raises(RuntimeError, match="catalog lookup timed out"):
            adapter.table_exists("entity", namespace=_NAMESPACE)

    def test_bootstrap_turns_the_propagated_exception_into_indeterminate_state(self):
        transport = _FakeTransport(exists_error=RuntimeError("catalog lookup timed out"))
        underlying = _FakeUnderlyingCapability()
        adapter = ClearinghouseBootstrapCapability(underlying, transport=transport)

        with pytest.raises(BootstrapIndeterminateStateError, match="entity"):
            bootstrap(adapter, namespace=_NAMESPACE)

        assert underlying.load_calls == []


# --------------------------------------------------------------------------
# 10: engine="spark" on every query call
# --------------------------------------------------------------------------


class TestEngineSparkOnEveryQuery:
    def test_table_partition_spec_passes_engine_spark(self):
        underlying = _FakeUnderlyingCapability(
            query_result=[("# Detailed Table Information", "", "")]
        )
        transport = _FakeTransport()
        adapter = ClearinghouseBootstrapCapability(underlying, transport=transport)

        adapter.table_partition_spec("entity", namespace=_NAMESPACE)

        assert len(underlying.query_calls) == 1
        sql, kwargs = underlying.query_calls[0]
        assert kwargs.get("engine") == "spark"
        assert sql == _describe_table_extended_sql("entity", _NAMESPACE)

    def test_query_result_flows_through_to_the_pure_dispatch(self):
        underlying = _FakeUnderlyingCapability(
            query_result=_describe_extended_rows(("Part 0", "entity_type"))
        )
        transport = _FakeTransport()
        adapter = ClearinghouseBootstrapCapability(underlying, transport=transport)

        result = adapter.table_partition_spec("entity", namespace=_NAMESPACE)
        assert result == ["entity_type"]
