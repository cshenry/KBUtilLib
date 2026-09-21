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

from kbutillib.domains.kbase.berdl.capability import (
    BerdlCapability,
    build_ingest_config,
)
from kbutillib.domains.kbase.berdl.clearinghouse_bootstrap_adapter import (
    ClearinghouseBootstrapCapability,
    PartitionSpecUnparseableError,
    _create_table_sql,
    _describe_table_extended_sql,
    _parse_describe_table_extended_partition_spec,
    _parse_partition_spec,
    _parse_show_create_table_partition_spec,
    _show_create_table_sql,
)
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    BootstrapIndeterminateStateError,
    bootstrap,
    table_configs,
)

_NAMESPACE = "clearinghouse"


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _FakeSpark:
    """Stands in for a Spark session: only ``sql``, recording statements.

    The adapter's schema-only ``load()`` path runs DDL on the session it
    resolved, so a bare ``object()`` no longer stands in for one -- every
    fake session in this module must be able to record a ``sql()`` call.
    """

    def __init__(self, *, sql_error=None):
        self.statements: list[str] = []
        self._sql_error = sql_error

    def sql(self, statement):
        self.statements.append(statement)
        if self._sql_error is not None:
            raise self._sql_error
        return []


class _ContractCheckingCapability:
    """A fake ``BerdlCapability`` that enforces the REAL ``load()`` contract.

    ``_FakeUnderlyingCapability.load`` accepts any kwargs, which is exactly
    why the ``bootstrap()`` <-> ``load()`` seam broke undetected until an
    on-pod run hit it on 2026-09-20: ``bootstrap()`` calls ``load()`` with
    no ``dataframes`` and no ``paths``, and the real
    ``BerdlCapability.load()`` routes that into ``build_ingest_config()``,
    which raises ``ValueError`` for bronze mode without a ``bronze_base``.
    This fake calls the REAL ``build_ingest_config`` so that contract is
    exercised off-pod, where ``BerdlCapability.load()`` itself cannot run.
    """

    def __init__(self):
        self.load_calls: list[dict] = []

    def load(self, **kwargs):
        self.load_calls.append(kwargs)
        build_ingest_config(
            kwargs["dataset"],
            kwargs["tables"],
            dataframes=kwargs.get("dataframes"),
            paths=kwargs.get("paths"),
        )
        return {"ingest_result": "ok"}


class _FakeTransport:
    """Stands in for ``InPodTransport``: only ``spark_session`` and
    ``table_exists`` -- the two members the adapter actually calls.
    """

    def __init__(self, spark=None, *, exists=True, exists_error=None):
        self._spark = spark if spark is not None else _FakeSpark()
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
        session = _FakeSpark()
        transport = _FakeTransport(session, exists=False)
        underlying = _FakeUnderlyingCapability()
        adapter = ClearinghouseBootstrapCapability(underlying, transport=transport)

        result = bootstrap(adapter, namespace=_NAMESPACE)

        assert result["dry_run"] is False
        assert all(t["action"] == "create" for t in result["tables"])
        # bootstrap() has no data source, so its load() is schema-only and
        # is served by DDL here rather than forwarded to the wrapped
        # capability -- see TestSchemaOnlyCreatePath.
        assert underlying.load_calls == []
        assert len(session.statements) == 15

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
        session = _FakeSpark()
        transport = _FakeTransport(session, exists=True)
        underlying = _FakeUnderlyingCapability()
        adapter = ClearinghouseBootstrapCapability(underlying, transport=transport)

        adapter.table_exists("entity", namespace=_NAMESPACE)
        adapter.load(
            dataset="clearinghouse",
            tables=[],
            namespace=_NAMESPACE,
            dataframes={"entity": object()},
        )

        assert transport.table_exists_calls[0][0] is session
        assert underlying.load_calls[0]["spark"] is session
        # Only resolved once, not once per call.
        assert transport.spark_session_calls == 1

    def test_schema_only_ddl_runs_on_the_probed_session(self):
        """The schema-only path must share session identity too.

        ``table_exists`` decides create-vs-skip and the ``CREATE TABLE``
        acts on that decision, so the two disagreeing about which session
        they are looking at is the same defect on the DDL path as on the
        forwarded one -- only with a table created in a catalog the probe
        never read.
        """
        session = _FakeSpark()
        transport = _FakeTransport(session, exists=False)
        underlying = _FakeUnderlyingCapability()
        adapter = ClearinghouseBootstrapCapability(underlying, transport=transport)

        adapter.table_exists("gene_entity", namespace=_NAMESPACE)
        adapter.load(
            dataset="clearinghouse",
            tables=[{"name": "gene_entity", "schema_sql": "entity_hash BINARY"}],
            namespace=_NAMESPACE,
        )

        assert transport.table_exists_calls[0][0] is session
        assert len(session.statements) == 1
        assert transport.spark_session_calls == 1
        # Nothing was forwarded: the wrapped capability was never called.
        assert underlying.load_calls == []

    def test_explicitly_supplied_spark_is_used_and_never_re_resolved(self):
        session = _FakeSpark()
        transport = _FakeTransport(exists=True)
        underlying = _FakeUnderlyingCapability()
        adapter = ClearinghouseBootstrapCapability(
            underlying, spark=session, transport=transport
        )

        adapter.table_exists("entity", namespace=_NAMESPACE)
        adapter.load(
            dataset="clearinghouse",
            tables=[],
            namespace=_NAMESPACE,
            dataframes={"entity": object()},
        )

        assert transport.table_exists_calls[0][0] is session
        assert underlying.load_calls[0]["spark"] is session
        assert transport.spark_session_calls == 0

    def test_load_ignores_a_caller_supplied_spark_kwarg(self):
        """The adapter owns session identity end-to-end -- a caller
        passing its own ``spark=`` into ``load()`` must not be able to
        desynchronize it from the session ``table_exists`` probes with.
        """
        session = _FakeSpark()
        other_session = _FakeSpark()
        transport = _FakeTransport(session, exists=True)
        underlying = _FakeUnderlyingCapability()
        adapter = ClearinghouseBootstrapCapability(underlying, transport=transport)

        adapter.table_exists("entity", namespace=_NAMESPACE)
        adapter.load(
            dataset="clearinghouse",
            tables=[],
            namespace=_NAMESPACE,
            spark=other_session,
            dataframes={"entity": object()},
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

    def test_emitters_quote_each_namespace_segment_separately(self):
        """One backtick pair PER dot-separated namespace segment.

        A production clearinghouse namespace is dotted
        (``kbaseincubator.clearinghouse``), and quoting it as a single
        identifier yields a two-part name whose first part contains a dot,
        which Spark does not resolve -- measured in-pod as defect D2
        (``agent-io/prds/berdl-smoke-verification/in-pod-results.md``),
        where ``` `ns`.`t1` ``` raised ``TABLE_OR_VIEW_NOT_FOUND`` while
        ``` `c`.`n`.`t1` ``` resolved.
        """
        sql = _describe_table_extended_sql(
            "gene_entity", "kbaseincubator.clearinghouse"
        )
        assert "`kbaseincubator`.`clearinghouse`.`gene_entity`" in sql

    def test_emitters_never_split_a_table_name_on_a_dot(self):
        """A dot inside a table NAME is part of the name, not a separator."""
        sql = _describe_table_extended_sql("my.table", "my.ns")
        assert "`my`.`ns`.`my.table`" in sql


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


# --------------------------------------------------------------------------
# 8: the schema-only create path (the bootstrap() <-> load() seam)
# --------------------------------------------------------------------------


class TestSchemaOnlyCreatePath:
    """The seam that blocked OP2 on 2026-09-20.

    ``bootstrap()`` takes no ``dataframes`` and no ``paths`` parameters,
    so every ``load()`` call it makes is schema-only by construction. The
    adapter used to forward those straight to ``BerdlCapability.load()``,
    whose ``build_ingest_config()`` raises ``ValueError: bronze mode (no
    'dataframes' given) requires 'paths' with a 'bronze_base'``. It now
    creates the tables with DDL instead.
    """

    def test_bootstrap_through_a_contract_checking_capability_no_longer_raises(self):
        """The regression test for the OP2 blocker.

        This drives the exact call ``bootstrap()`` makes into a fake whose
        ``load()`` runs the REAL ``build_ingest_config``. Before the fix
        this raised ``ValueError`` about bronze mode and ``bronze_base``;
        after it, ``load()`` is never reached at all.
        """
        session = _FakeSpark()
        transport = _FakeTransport(session, exists=False)
        underlying = _ContractCheckingCapability()
        adapter = ClearinghouseBootstrapCapability(underlying, transport=transport)

        result = bootstrap(adapter, namespace="kbaseincubator.clearinghouse")

        assert underlying.load_calls == []
        assert result["load_result"]["schema_only"] is True
        assert len(session.statements) == 15

    def test_the_old_forwarding_behaviour_really_did_raise(self):
        """Guards the regression test above against passing vacuously.

        If ``build_ingest_config`` ever stopped rejecting a no-source
        call, the test above would pass whether or not the adapter was
        fixed. This asserts the rejection is still real.
        """
        with pytest.raises(ValueError, match="bronze_base"):
            build_ingest_config(
                "clearinghouse",
                [{"name": "gene_entity", "mode": "append"}],
            )

    def test_every_clearinghouse_table_gets_exactly_one_statement(self):
        session = _FakeSpark()
        transport = _FakeTransport(session, exists=False)
        adapter = ClearinghouseBootstrapCapability(
            _FakeUnderlyingCapability(), transport=transport
        )

        result = adapter.load(
            dataset="clearinghouse",
            tables=table_configs(),
            namespace="kbaseincubator.clearinghouse",
        )

        assert [report["name"] for report in result["tables"]] == [
            config["name"] for config in table_configs()
        ]
        assert len(session.statements) == 15
        assert all(
            statement.startswith("CREATE TABLE IF NOT EXISTS")
            for statement in session.statements
        )

    def test_ddl_declares_schema_sql_verbatim_and_uses_iceberg(self):
        sql = _create_table_sql(
            {"name": "gene_entity", "schema_sql": "entity_hash BINARY, src STRING"},
            "kbaseincubator.clearinghouse",
        )
        assert sql == (
            "CREATE TABLE IF NOT EXISTS "
            "`kbaseincubator`.`clearinghouse`.`gene_entity` "
            "(entity_hash BINARY, src STRING) USING iceberg"
        )

    def test_ddl_carries_partitioned_by_when_the_config_partitions(self):
        sql = _create_table_sql(
            {
                "name": "gene_entity",
                "schema_sql": "entity_hash BINARY",
                "partition_by": "standardizer_version",
            },
            "kbaseincubator.clearinghouse",
        )
        assert sql.endswith("USING iceberg PARTITIONED BY (`standardizer_version`)")

    def test_ddl_omits_partitioned_by_when_the_config_does_not_partition(self):
        sql = _create_table_sql(
            {"name": "genome_entity", "schema_sql": "entity_hash BINARY"},
            "kbaseincubator.clearinghouse",
        )
        assert "PARTITIONED BY" not in sql

    def test_partition_column_order_is_preserved(self):
        """Partition column ORDER is part of an Iceberg partition spec."""
        sql = _create_table_sql(
            {
                "name": "t",
                "schema_sql": "a STRING, b STRING",
                "partition_by": ["b", "a"],
            },
            "ns",
        )
        assert sql.endswith("PARTITIONED BY (`b`, `a`)")

    def test_entity_hash_is_declared_binary_on_every_generated_statement(self):
        """The BINARY declaration is the point of emitting DDL at all.

        Runbook acceptance step 2.3 exists because an INFERRED schema can
        silently demote ``entity_hash`` to ``STRING``. Declaring it in the
        DDL is what makes that demotion the catalog's problem rather than
        an unmeasured gamble.
        """
        for config in table_configs():
            sql = _create_table_sql(config, "kbaseincubator.clearinghouse")
            assert "entity_hash BINARY" in sql

    def test_a_config_without_schema_sql_raises_before_anything_runs(self):
        session = _FakeSpark()
        transport = _FakeTransport(session, exists=False)
        adapter = ClearinghouseBootstrapCapability(
            _FakeUnderlyingCapability(), transport=transport
        )

        with pytest.raises(ValueError, match="schema_sql"):
            adapter.load(
                dataset="clearinghouse",
                tables=[
                    {"name": "good", "schema_sql": "a STRING"},
                    {"name": "bad"},
                ],
                namespace="kbaseincubator.clearinghouse",
            )

        # The whole batch failed: not even the well-formed table ran.
        assert session.statements == []

    def test_namespace_is_never_created_by_this_path(self):
        """Nothing in the sanctioned write path creates a namespace
        (runbook 2.0b), and this path must not start.
        """
        session = _FakeSpark()
        transport = _FakeTransport(session, exists=False)
        adapter = ClearinghouseBootstrapCapability(
            _FakeUnderlyingCapability(), transport=transport
        )

        adapter.load(
            dataset="clearinghouse",
            tables=table_configs(),
            namespace="kbaseincubator.clearinghouse",
        )

        assert not any(
            "CREATE NAMESPACE" in statement.upper()
            or "CREATE DATABASE" in statement.upper()
            for statement in session.statements
        )

    def test_a_data_bearing_load_still_forwards_untouched(self):
        session = _FakeSpark()
        transport = _FakeTransport(session, exists=True)
        underlying = _FakeUnderlyingCapability()
        adapter = ClearinghouseBootstrapCapability(underlying, transport=transport)
        frames = {"gene_entity": object()}

        adapter.load(
            dataset="clearinghouse",
            tables=[{"name": "gene_entity", "mode": "append"}],
            namespace="kbaseincubator.clearinghouse",
            dataframes=frames,
        )

        assert len(underlying.load_calls) == 1
        assert underlying.load_calls[0]["dataframes"] is frames
        assert session.statements == []

    def test_a_bronze_mode_load_still_forwards_untouched(self):
        session = _FakeSpark()
        transport = _FakeTransport(session, exists=True)
        underlying = _FakeUnderlyingCapability()
        adapter = ClearinghouseBootstrapCapability(underlying, transport=transport)

        adapter.load(
            dataset="clearinghouse",
            tables=[{"name": "gene_entity", "bronze_path": "x", "format": "parquet"}],
            namespace="kbaseincubator.clearinghouse",
            paths={"bronze_base": "s3a://bucket/bronze"},
        )

        assert len(underlying.load_calls) == 1
        assert session.statements == []


# --------------------------------------------------------------------------
# 9: the partitioning shape the BERDL cluster actually emits
# --------------------------------------------------------------------------


def _berdl_describe_rows(*partition_columns: str, metadata_column: bool = True):
    """Build DESCRIBE TABLE EXTENDED rows in the shape measured ON-POD.

    Transcribed from the OP2 acceptance run of 2026-09-20 (reply
    ``trigger-inbox/replies/op2-create-clearinghouse-tables-ROUND2.json``,
    ``gene_entity``): a ``# Partition Information`` header, a
    ``# col_name`` label row, then the partition columns as PLAIN rows --
    NOT the numbered ``Part <N>`` rows this parser was first written for.
    """
    rows = [
        ("entity_hash", "binary", None),
        ("entity_type", "string", None),
        ("standardizer_version", "string", None),
        ("observed_at", "timestamp", None),
        ("ingest_batch_id", "string", None),
    ]
    if partition_columns:
        rows.append(("# Partition Information", "", ""))
        rows.append(("# col_name", "data_type", "comment"))
        for column in partition_columns:
            rows.append((column, "string", None))
    rows.append(("# Metadata Columns", "", ""))
    rows.append(("_spec_id", "int", ""))
    if metadata_column:
        # Every Iceberg table on this cluster exposes a ``_partition``
        # metadata column: ``struct<col:type,...>`` when partitioned,
        # empty ``struct<>`` when NOT (measured on-pod 2026-09-20).
        struct = ",".join(f"{c}:string" for c in partition_columns)
        rows.append(("_partition", f"struct<{struct}>", ""))
    rows.append(("# Detailed Table Information", "", ""))
    rows.append(("Name", f"{_NAMESPACE}.gene_entity", None))
    return rows


class TestBerdlPartitionInformationShape:
    """The live cluster's shape, and the silent ``[]`` it used to produce.

    Measured on-pod 2026-09-20: ``table_partition_spec`` returned ``[]``
    -- "unpartitioned" -- for all nine genuinely partitioned clearinghouse
    tables, because neither parser recognised this shape. ``bootstrap()``
    reads ``[]`` as unpartitioned, so the partition-spec safety check
    passed while having determined nothing, and the first idempotent
    re-run would have raised a FALSE mismatch (``[]`` vs
    ``['standardizer_version']``).
    """

    def test_single_partition_column_is_read(self):
        rows = _berdl_describe_rows("standardizer_version")
        assert _parse_partition_spec(
            rows, name="gene_entity", namespace=_NAMESPACE
        ) == ["standardizer_version"]

    def test_it_no_longer_silently_reports_unpartitioned(self):
        """The regression assertion: this exact input used to give []."""
        rows = _berdl_describe_rows("standardizer_version")
        assert (
            _parse_partition_spec(rows, name="gene_entity", namespace=_NAMESPACE) != []
        )

    def test_partition_column_order_is_preserved(self):
        rows = _berdl_describe_rows("source", "standardizer_version")
        assert _parse_partition_spec(
            rows, name="gene_result", namespace=_NAMESPACE
        ) == ["source", "standardizer_version"]

    def test_a_genuinely_unpartitioned_table_still_reports_empty(self):
        """``[]`` must stay reachable -- six clearinghouse tables are
        genuinely unpartitioned, and turning every ``[]`` into a raise
        would break them.

        Regression for the empty-``struct<>`` bug (found on-pod
        2026-09-21): the fixture carries the real ``_partition struct<>``
        metadata column an unpartitioned table exposes. Before the fix,
        ``value.startswith('struct<')`` matched ``struct<>`` and the empty
        struct was misread as partitioning evidence, so this raised
        ``PartitionSpecUnparseableError`` instead of returning ``[]``.
        """
        rows = _berdl_describe_rows()
        assert any(
            r[0] == "_partition" and r[1] == "struct<>" for r in rows
        ), "fixture must model the empty _partition struct<> an unpartitioned table emits"
        assert (
            _parse_partition_spec(rows, name="genome_entity", namespace=_NAMESPACE)
            == []
        )

    def test_empty_partition_struct_is_not_evidence_of_partitioning(self):
        """An empty ``_partition struct<>`` alone (no partitioning block)
        must read as unpartitioned, not raise -- the exact 2026-09-21
        on-pod failure of the six ``*_entity``/``*_content`` tables.
        """
        rows = _berdl_describe_rows("standardizer_version", metadata_column=True)
        # Sanity: with a partition column the struct is non-empty and read.
        assert _parse_partition_spec(
            rows, name="protein_entity", namespace=_NAMESPACE
        ) == ["standardizer_version"]
        # And the empty-struct case does not raise.
        rows = _berdl_describe_rows()
        assert _parse_describe_table_extended_partition_spec(
            rows, name="genome_entity", namespace=_NAMESPACE
        ) == []

    def test_metadata_column_alone_raises_rather_than_reporting_empty(self):
        """A ``_partition`` struct column is independent evidence the table
        IS partitioned. If it is present and no partitioning block was
        understood, reporting ``[]`` would be the silent pass again.
        """
        rows = [
            ("entity_hash", "binary", None),
            ("# Some Unknown Future Header", "", ""),
            ("standardizer_version", "string", None),
            ("# Metadata Columns", "", ""),
            ("_partition", "struct<standardizer_version:string>", ""),
            ("# Detailed Table Information", "", ""),
            ("Name", f"{_NAMESPACE}.gene_entity", None),
        ]
        with pytest.raises(PartitionSpecUnparseableError, match="_partition"):
            _parse_partition_spec(rows, name="gene_entity", namespace=_NAMESPACE)

    def test_an_empty_partition_information_block_raises(self):
        rows = [
            ("entity_hash", "binary", None),
            ("# Partition Information", "", ""),
            ("# col_name", "data_type", "comment"),
            ("# Detailed Table Information", "", ""),
            ("Name", f"{_NAMESPACE}.gene_entity", None),
        ]
        with pytest.raises(PartitionSpecUnparseableError):
            _parse_partition_spec(rows, name="gene_entity", namespace=_NAMESPACE)

    def test_a_transform_in_the_plain_shape_raises(self):
        rows = [
            ("entity_hash", "binary", None),
            ("# Partition Information", "", ""),
            ("# col_name", "data_type", "comment"),
            ("bucket(256, entity_hash)", "int", None),
            ("# Detailed Table Information", "", ""),
            ("Name", f"{_NAMESPACE}.gene_entity", None),
        ]
        with pytest.raises(PartitionSpecUnparseableError, match="transform"):
            _parse_partition_spec(rows, name="gene_entity", namespace=_NAMESPACE)

    def test_the_numbered_part_shape_still_works(self):
        """The original shape is still handled -- this fix adds a shape,
        it does not replace one.
        """
        rows = _describe_extended_rows(("Part 0", "standardizer_version"))
        assert _parse_partition_spec(
            rows, name="gene_entity", namespace=_NAMESPACE
        ) == ["standardizer_version"]

    def test_bootstrap_rerun_against_the_real_shape_takes_the_append_branch(self):
        """End-to-end: the false-mismatch that would have blocked re-runs.

        Before the fix this raised ``BootstrapPartitionSpecMismatchError``
        comparing ``[]`` against ``['standardizer_version']``.
        """
        session = _FakeSpark()
        transport = _FakeTransport(session, exists=True)
        underlying = _FakeUnderlyingCapability(
            query_result=_berdl_describe_rows("standardizer_version")
        )
        adapter = ClearinghouseBootstrapCapability(underlying, transport=transport)

        result = bootstrap(
            adapter,
            namespace=_NAMESPACE,
            tables=[
                {
                    "name": "gene_entity",
                    "schema_sql": "entity_hash BINARY",
                    "partition_by": "standardizer_version",
                }
            ],
        )

        assert result["tables"][0]["action"] == "append"
        assert result["tables"][0]["actual_partition_by"] == ["standardizer_version"]
