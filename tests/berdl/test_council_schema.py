"""Unit tests for kbutillib.domains.kbase.berdl.council_schema.

Pure logic plus one REAL local SQL sink, no network and no live BERDL pod.

Two things are worth stating about the sink, because the
no-injection bar for this module's task turns on it. The sibling tests for
``clearinghouse_schema.bootstrap()``
(``tests/berdl/test_clearinghouse_bootstrap.py``) drive it against a
hand-built ``_FakeCapability`` whose ``load()`` records its arguments and
writes nothing -- there is NO local engine or SQL sink anywhere in that
module to mirror. So ``TestBootstrapAgainstRealSqliteSink`` below goes one
step further than mirroring: :class:`_SqliteSinkCapability` EXECUTES each
config's ``schema_sql`` as real DDL against a real SQLAlchemy/SQLite
database and answers ``table_exists`` / ``table_partition_spec`` by reading
that database back, so ``bootstrap()`` runs through its REAL default code
path (``tables`` defaulted to ``table_configs()``) and the nine tables are
asserted to exist with the declared columns FROM THE CATALOG, not from the
config dict the test passed in.

What that does NOT prove: SQLite is not Iceberg. It parses the DDL and
materialises the columns, which is enough to catch a malformed
``schema_sql``, a duplicate column, or a column list that disagrees with the
declarations -- but it has no partition specs (the sink records
``partition_by`` alongside the table and reports it back, which is a
convention of the sink, not a property of SQLite), no Iceberg table
properties, and no ``data_lakehouse_ingest`` type map. Live acceptance of
this namespace on kbhub is a separate, operator-owned step; see KBUtilLib#91
and the task work-record.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from kbutillib.domains.ai.council.store import schema as store_schema
from kbutillib.domains.kbase.berdl import clearinghouse_schema
from kbutillib.domains.kbase.berdl.council_schema import (
    ARCHIVED_TABLES,
    COUNCIL_ENTITY_HASH_COLUMN,
    COUNCIL_NAMESPACE,
    IDENTITY_COLUMN_NAMES,
    JOB_DISPOSITION_TABLE,
    JOB_DISPOSITIONS,
    NAMESPACE,
    NOT_ARCHIVED,
    PARTITION_COLUMN,
    ROLE_BRIEF_TABLE,
    ROLE_BRIEF_UNEXTRACTABLE,
    TENANT,
    BootstrapIndeterminateStateError,
    BootstrapPartitionSpecMismatchError,
    ContradictoryQueryNameError,
    attempt_query_name,
    bootstrap,
    column_names,
    decode_entity_hash,
    encode_entity_hash,
    item_query_name,
    table_columns,
    table_configs,
    table_name,
    table_primary_key,
)

#: The five lease/queue columns of ``calls`` that must NOT be archived.
_LEASE_COLUMNS = (
    "not_before",
    "claim_token",
    "claimed_by",
    "claimed_at",
    "lease_generation",
)

#: The three tables that partition, and the only ones that may.
_PARTITIONED_TABLES = ("items", "calls", "attempts")


def _columns_from_schema_sql(schema_sql: str) -> dict[str, str]:
    """Parse a ``"name TYPE, name TYPE, ..."`` DDL fragment into a dict.

    Same helper, same shape, as ``tests/berdl/test_clearinghouse_schema.py``
    -- the two modules render ``schema_sql`` through identical code, so the
    parse is identical too.
    """
    columns = {}
    for part in schema_sql.split(","):
        name, sql_type = part.strip().split(" ", 1)
        columns[name] = sql_type
    return columns


def _configs_by_name() -> dict[str, dict]:
    return {table["name"]: table for table in table_configs()}


def _store_column_names(table: str) -> set[str]:
    """Column names of one table of the LIVE council store schema."""
    return {column.name for column in store_schema.metadata.tables[table].columns}


# --------------------------------------------------------------------------
# Module constants and the table set
# --------------------------------------------------------------------------


class TestModuleConstants:
    def test_tenant_and_namespace(self):
        assert TENANT == "kbaseincubator"
        assert NAMESPACE == "council"
        assert COUNCIL_NAMESPACE == "kbaseincubator.council"

    def test_the_tenant_matches_the_clearinghouse_tenant(self):
        # The two namespaces must sit in one catalog to be joinable at all.
        assert TENANT == clearinghouse_schema.TENANT

    def test_nine_archived_tables(self):
        assert len(ARCHIVED_TABLES) == 9
        assert ARCHIVED_TABLES == (
            "query_types",
            "jobs",
            "items",
            "calls",
            "attempts",
            "endpoint_state",
            "workers",
            "job_disposition",
            "role_brief",
        )

    def test_seven_archived_tables_mirror_store_tables_and_two_are_new(self):
        store_tables = set(store_schema.TABLE_NAMES)
        mirrored = [t for t in ARCHIVED_TABLES if t in store_tables]
        new = [t for t in ARCHIVED_TABLES if t not in store_tables]
        assert len(mirrored) == 7
        assert new == [JOB_DISPOSITION_TABLE, ROLE_BRIEF_TABLE]

    def test_schema_version_does_not_publish(self):
        assert NOT_ARCHIVED == ("schema_version",)
        assert "schema_version" in store_schema.TABLE_NAMES
        assert "schema_version" not in ARCHIVED_TABLES
        assert "schema_version" not in _configs_by_name()

    def test_asking_for_schema_version_names_the_reason(self):
        with pytest.raises(ValueError, match="deliberately does not publish"):
            table_name("schema_version")

    def test_every_store_table_except_schema_version_is_archived(self):
        unarchived = set(store_schema.TABLE_NAMES) - set(ARCHIVED_TABLES)
        assert unarchived == {"schema_version"}

    def test_job_dispositions_are_the_declared_five(self):
        assert JOB_DISPOSITIONS == (
            "published",
            "pilot",
            "trial",
            "cancelled",
            "superseded",
        )

    def test_role_brief_has_an_unextractable_sentinel(self):
        # A spec shape the extractor cannot read must be RECORDED as
        # unextractable, never guessed at -- so the sentinel has to exist
        # and must not be an empty string (indistinguishable from "no
        # mission text was given").
        assert ROLE_BRIEF_UNEXTRACTABLE
        assert ROLE_BRIEF_UNEXTRACTABLE != ""


class TestTableName:
    def test_resolves_every_archived_table_to_itself(self):
        for table in ARCHIVED_TABLES:
            assert table_name(table) == table

    def test_raises_on_unknown_table(self):
        with pytest.raises(ValueError, match="unknown council table"):
            table_name("daemon_lease")


# --------------------------------------------------------------------------
# The column tuples are the single source of truth
# --------------------------------------------------------------------------


class TestColumnDeclarationsAreTheSingleSourceOfTruth:
    def test_table_configs_schema_sql_is_rendered_from_table_columns(self):
        configs = _configs_by_name()
        for table in ARCHIVED_TABLES:
            expected = ", ".join(
                f"{name} {sql_type}" for name, sql_type in table_columns(table)
            )
            assert configs[table]["schema_sql"] == expected

    def test_schema_sql_parses_back_to_exactly_table_columns(self):
        configs = _configs_by_name()
        for table in ARCHIVED_TABLES:
            parsed = _columns_from_schema_sql(configs[table]["schema_sql"])
            assert parsed == dict(table_columns(table))
            # Order too, not just membership.
            assert tuple(parsed) == column_names(table)

    def test_column_names_agrees_with_table_columns(self):
        for table in ARCHIVED_TABLES:
            assert column_names(table) == tuple(
                name for name, _ in table_columns(table)
            )

    def test_no_table_declares_a_duplicate_column(self):
        for table in ARCHIVED_TABLES:
            names = column_names(table)
            assert len(names) == len(set(names)), table

    def test_table_configs_returns_a_fresh_list_each_call(self):
        first, second = table_configs(), table_configs()
        assert first == second
        assert first is not second
        first[0]["name"] = "mutated"
        assert table_configs()[0]["name"] == "query_types"

    def test_no_config_value_anywhere_contains_a_parenthesis(self):
        # Same guard as clearinghouse_schema: a parenthesis would mean an
        # Iceberg partition TRANSFORM expression leaked into a config, and
        # the write path passes partition_by through as a bare column name.
        def _walk(value):
            if isinstance(value, str):
                assert "(" not in value and ")" not in value, value
            elif isinstance(value, dict):
                for v in value.values():
                    _walk(v)
            elif isinstance(value, (list, tuple)):
                for v in value:
                    _walk(v)

        _walk(table_configs())

    def test_every_config_has_exactly_name_schema_sql_and_maybe_partition_by(self):
        for config in table_configs():
            assert set(config) <= {"name", "schema_sql", "partition_by"}
            assert set(config) >= {"name", "schema_sql"}

    def test_every_config_name_equals_the_table_name_resolver(self):
        for config in table_configs():
            assert config["name"] == table_name(config["name"])

    def test_configs_are_emitted_in_archived_tables_order(self):
        assert tuple(c["name"] for c in table_configs()) == ARCHIVED_TABLES


# --------------------------------------------------------------------------
# Identity columns and primary keys
# --------------------------------------------------------------------------


class TestIdentityColumns:
    def test_every_archived_table_leads_with_the_six_identity_columns(self):
        assert IDENTITY_COLUMN_NAMES == (
            "store_id",
            "snapshot_id",
            "snapshot_seq",
            "store_host",
            "loaded_at",
            "load_batch_id",
        )
        for table in ARCHIVED_TABLES:
            assert column_names(table)[:6] == IDENTITY_COLUMN_NAMES

    def test_the_identity_block_is_byte_identical_across_all_nine_tables(self):
        heads = {table_columns(table)[:6] for table in ARCHIVED_TABLES}
        assert len(heads) == 1

    def test_the_two_new_tables_do_not_redeclare_store_id(self):
        # Their spec names store_id, but it arrives with the identity block;
        # declaring it twice would be a duplicate column in the DDL.
        for table in (JOB_DISPOSITION_TABLE, ROLE_BRIEF_TABLE):
            assert column_names(table).count("store_id") == 1


class TestPrimaryKeys:
    def test_every_primary_key_leads_with_store_id_and_snapshot_id(self):
        for table in ARCHIVED_TABLES:
            assert table_primary_key(table)[:2] == ("store_id", "snapshot_id")

    @pytest.mark.parametrize(
        "table,expected_tail",
        [
            ("query_types", ("name", "version")),
            ("jobs", ("job_id",)),
            ("items", ("job_id", "item_id")),
            ("calls", ("call_id",)),
            ("attempts", ("call_id", "attempt_no")),
            ("endpoint_state", ("user", "endpoint")),
            ("workers", ("worker_id",)),
            ("job_disposition", ("job_id",)),
            ("role_brief", ("query_name", "query_version", "role")),
        ],
    )
    def test_primary_key_tail_is_the_source_tables_own_key(self, table, expected_tail):
        assert table_primary_key(table) == ("store_id", "snapshot_id") + expected_tail

    def test_every_primary_key_column_is_a_declared_column(self):
        for table in ARCHIVED_TABLES:
            declared = set(column_names(table))
            assert set(table_primary_key(table)) <= declared, table

    def test_the_seven_mirrored_tables_reuse_the_stores_own_primary_key(self):
        for table in ARCHIVED_TABLES:
            if table not in store_schema.metadata.tables:
                continue
            store_pk = tuple(
                column.name
                for column in store_schema.metadata.tables[table].primary_key.columns
            )
            archived_tail = table_primary_key(table)[2:]
            if table == "items":
                # items.entity_hash is renamed, but its key is not a hash.
                assert archived_tail == store_pk
            else:
                assert set(archived_tail) == set(store_pk), table

    def test_primary_key_is_not_carried_in_the_load_configs(self):
        # The write path reads only name/schema_sql/partition_by; an unread
        # 'primary_key' key in the config would be a claim it does not honour.
        for config in table_configs():
            assert "primary_key" not in config


# --------------------------------------------------------------------------
# The dropped columns
# --------------------------------------------------------------------------


class TestDroppedColumns:
    def test_the_five_lease_columns_are_absent_from_calls(self):
        archived = set(column_names("calls"))
        for lease_column in _LEASE_COLUMNS:
            assert lease_column in _store_column_names("calls"), lease_column
            assert lease_column not in archived, lease_column

    def test_the_five_lease_columns_are_absent_from_every_table(self):
        for table in ARCHIVED_TABLES:
            assert not set(column_names(table)) & set(_LEASE_COLUMNS), table

    def test_items_item_order_is_absent(self):
        assert "item_order" in _store_column_names("items")
        assert "item_order" not in column_names("items")

    def test_jobs_priority_is_absent(self):
        assert "priority" in _store_column_names("jobs")
        assert "priority" not in column_names("jobs")

    def test_endpoint_state_drops_exactly_the_two_halt_columns(self):
        archived = set(column_names("endpoint_state")) - set(IDENTITY_COLUMN_NAMES)
        assert archived == _store_column_names("endpoint_state") - {
            "halt_kind",
            "halt_message",
        }

    def test_workers_archives_only_the_four_provenance_columns(self):
        archived = tuple(
            name
            for name in column_names("workers")
            if name not in IDENTITY_COLUMN_NAMES
        )
        assert archived == ("worker_id", "host", "version", "started_at")

    def test_workers_keeps_version_even_though_the_snapshot_has_only_nulls(self):
        # All six workers.version values in the 2026-10-23 snapshot are
        # NULL. The column publishes anyway and the NULLs are preserved;
        # dropping it would lose the only build-provenance question an
        # archive gets asked.
        assert "version" in column_names("workers")

    def test_query_types_archives_all_seven_store_columns(self):
        archived = set(column_names("query_types")) - set(IDENTITY_COLUMN_NAMES)
        assert archived == _store_column_names("query_types")

    def test_jobs_archives_every_store_column_but_priority(self):
        archived = set(column_names("jobs")) - set(IDENTITY_COLUMN_NAMES)
        assert archived == _store_column_names("jobs") - {"priority"}

    def test_calls_archives_every_store_column_but_the_five_lease_columns(self):
        archived = set(column_names("calls")) - set(IDENTITY_COLUMN_NAMES)
        assert archived == _store_column_names("calls") - set(_LEASE_COLUMNS)

    def test_attempts_archives_every_store_column_plus_query_name(self):
        archived = set(column_names("attempts")) - set(IDENTITY_COLUMN_NAMES)
        assert archived == _store_column_names("attempts") | {"query_name"}

    def test_no_mirrored_table_invents_a_column_the_store_does_not_have(self):
        # Every archived source column must either exist in the store table,
        # be an identity column, or be one of the two DOCUMENTED additions
        # (the rename on items, the denormalised query_name).
        documented_additions = {
            "items": {COUNCIL_ENTITY_HASH_COLUMN, "query_name"},
            "attempts": {"query_name"},
        }
        for table in ARCHIVED_TABLES:
            if table not in store_schema.metadata.tables:
                continue
            unexplained = (
                set(column_names(table))
                - set(IDENTITY_COLUMN_NAMES)
                - _store_column_names(table)
                - documented_additions.get(table, set())
            )
            assert unexplained == set(), (table, unexplained)


# --------------------------------------------------------------------------
# The rename, and the two denormalisations
# --------------------------------------------------------------------------


class TestCouncilEntityHashRename:
    def test_items_exposes_council_entity_hash(self):
        assert COUNCIL_ENTITY_HASH_COLUMN == "council_entity_hash"
        assert COUNCIL_ENTITY_HASH_COLUMN in column_names("items")

    def test_items_does_not_expose_a_bare_entity_hash(self):
        # It matched the clearinghouse entity_hash 0 of 19,268 times. A bare
        # entity_hash column in a BERDL namespace is an invitation to join.
        assert "entity_hash" in _store_column_names("items")
        assert "entity_hash" not in column_names("items")

    def test_items_data_hash_is_not_renamed(self):
        assert "data_hash" in column_names("items")


class TestDenormalisedQueryName:
    def test_items_exposes_query_name_although_the_store_does_not(self):
        assert "query_name" not in _store_column_names("items")
        assert "query_name" in column_names("items")

    def test_attempts_exposes_query_name_although_the_store_does_not(self):
        assert "query_name" not in _store_column_names("attempts")
        assert "query_name" in column_names("attempts")

    def test_item_query_name_is_the_jobs_value(self):
        assert item_query_name(job_query_name="go_mf") == "go_mf"

    def test_item_query_name_passes_a_missing_job_through_as_none(self):
        assert item_query_name(job_query_name=None) is None

    def test_attempt_query_name_prefers_the_owning_call(self):
        assert (
            attempt_query_name(call_query_name="go_mf", job_query_name="go_mf")
            == "go_mf"
        )

    def test_attempt_query_name_falls_back_to_the_job_for_a_legacy_null_call(self):
        assert (
            attempt_query_name(call_query_name=None, job_query_name="go_mf") == "go_mf"
        )

    def test_attempt_query_name_rejects_a_contradiction_rather_than_preferring(self):
        with pytest.raises(ContradictoryQueryNameError) as excinfo:
            attempt_query_name(
                call_query_name="go_mf", job_query_name="reaction_pickup"
            )
        message = str(excinfo.value)
        # Both values must be named, so the operator can see the conflict.
        assert "go_mf" in message
        assert "reaction_pickup" in message

    def test_attempt_query_name_with_both_null_is_null_not_an_error(self):
        assert attempt_query_name(call_query_name=None, job_query_name=None) is None


# --------------------------------------------------------------------------
# live, and partitioning
# --------------------------------------------------------------------------


class TestLiveIsArchivedNotFiltered:
    def test_calls_exposes_live(self):
        assert "live" in column_names("calls")

    def test_live_is_declared_boolean(self):
        assert dict(table_columns("calls"))["live"] == "BOOLEAN"

    def test_calls_keeps_the_columns_that_make_a_superseded_row_readable(self):
        # ux_calls_one_live is partial over these five WHERE live, so the
        # superseded rows behind the live one are only interpretable if all
        # five plus live survive the archive.
        for column in (
            "job_id",
            "item_id",
            "role",
            "seat_name",
            "upstream_digest",
            "live",
        ):
            assert column in column_names("calls"), column

    def test_calls_keeps_reuse_and_cost_columns(self):
        # A reused call carries a full copy of reply_text with fresh_usage
        # zeroed, so cost must be summed from fresh_usage and the reuse edge
        # must stay readable.
        for column in ("reused_from", "fresh_usage", "reply_text", "reply_parsed"):
            assert column in column_names("calls"), column


class TestPartitioning:
    def test_partition_by_is_set_on_exactly_calls_items_and_attempts(self):
        configs = _configs_by_name()
        partitioned = {
            name for name, config in configs.items() if "partition_by" in config
        }
        assert partitioned == set(_PARTITIONED_TABLES)

    def test_each_partitioned_table_partitions_on_query_name(self):
        configs = _configs_by_name()
        assert PARTITION_COLUMN == "query_name"
        for table in _PARTITIONED_TABLES:
            assert configs[table]["partition_by"] == PARTITION_COLUMN

    def test_the_other_six_omit_the_key_entirely_rather_than_emitting_a_falsy_value(
        self,
    ):
        configs = _configs_by_name()
        for table in ARCHIVED_TABLES:
            if table in _PARTITIONED_TABLES:
                continue
            assert "partition_by" not in configs[table], table

    def test_every_partition_column_is_a_declared_bare_stored_column(self):
        for config in table_configs():
            partition_by = config.get("partition_by")
            if partition_by is None:
                continue
            assert isinstance(partition_by, str)
            assert partition_by in column_names(config["name"])

    def test_no_hash_column_is_ever_a_partition_key(self):
        # A good hash scatters uniformly, so bucketing on one buys zero read
        # pruning -- the same rule clearinghouse_schema holds to.
        hash_columns = {
            COUNCIL_ENTITY_HASH_COLUMN,
            "entity_hash",
            "data_hash",
            "prompt_hash",
            "snapshot_id",
            "route_fingerprint",
            "roster_fingerprint",
        }
        for config in table_configs():
            assert config.get("partition_by") not in hash_columns


# --------------------------------------------------------------------------
# The hash seam
# --------------------------------------------------------------------------


class TestHashSeam:
    def test_the_seam_is_the_clearinghouses_own_implementation(self):
        # Deliberately re-exported rather than re-implemented: one
        # normalisation rule, one implementation, no drift between the two
        # namespaces.
        assert encode_entity_hash is clearinghouse_schema.encode_entity_hash
        assert decode_entity_hash is clearinghouse_schema.decode_entity_hash

    def test_uppercase_hex_normalises_to_the_stored_lowercase_form(self):
        digest = "9f" + "a" * 62
        assert encode_entity_hash(digest.upper()) == digest

    def test_round_trip_of_a_real_snapshot_council_hash(self):
        # A council items.entity_hash as measured in the 2026-10-23
        # snapshot: 64 lowercase hex characters, the same stored form the
        # clearinghouse settled on.
        digest = "9f6267134ad073eeef96" + "0" * 44
        assert decode_entity_hash(encode_entity_hash(digest)) == digest

    def test_the_seam_refuses_the_empty_upstream_digest_default(self):
        # calls.upstream_digest defaults to '' in the store and is NOT a
        # digest, which is exactly why it must never be passed through here.
        assert dict(table_columns("calls"))["upstream_digest"] == "STRING"
        with pytest.raises(ValueError):
            encode_entity_hash("")


class TestNoBinaryColumnSurvives:
    def test_no_config_declares_a_binary_column(self):
        # The retired BINARY entity_hash could be created but never written:
        # data_lakehouse_ingest's type map has no BINARY. Same rule here.
        for config in table_configs():
            assert "BINARY" not in config["schema_sql"].upper()

    def test_every_declared_sql_type_is_one_of_the_five_allowed(self):
        allowed = {"STRING", "INT", "BIGINT", "BOOLEAN", "TIMESTAMP"}
        for table in ARCHIVED_TABLES:
            for name, sql_type in table_columns(table):
                assert sql_type in allowed, (table, name, sql_type)


# --------------------------------------------------------------------------
# bootstrap() against a REAL local SQLite sink
# --------------------------------------------------------------------------


class _SqliteSinkCapability:
    """A ``bootstrap()`` capability backed by a REAL SQLAlchemy/SQLite database.

    This is not a stub of ``bootstrap()``'s collaborator: ``load()`` takes
    each config's ``schema_sql`` and EXECUTES it as ``CREATE TABLE`` DDL
    against a live engine, and ``table_exists`` / ``table_partition_spec``
    answer by reading that database back through SQLAlchemy's inspector. A
    malformed ``schema_sql``, a duplicate column, or a column list that
    disagrees with the declarations fails here for real.

    SQLite has no partition specs, so the sink RECORDS each created table's
    ``partition_by`` in its own ``_partition_spec`` side table and reports it
    back from there. That round trip is a convention of this sink, not a
    property of SQLite -- it is what lets the idempotency and partition-guard
    paths be exercised at all off-pod, and it is the one place this fixture
    is weaker than Iceberg.
    """

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine
        self.load_calls: list[dict] = []
        with self._engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS _partition_spec "
                "(table_name TEXT PRIMARY KEY, partition_by TEXT)"
            )

    # -- read-only probes ------------------------------------------------

    def _physical_name(self, name: str, namespace: str) -> str:
        return f"{namespace}__{name}"

    def table_exists(self, name, *, namespace):
        inspector = sa.inspect(self._engine)
        return inspector.has_table(self._physical_name(name, namespace))

    def table_partition_spec(self, name, *, namespace):
        physical = self._physical_name(name, namespace)
        with self._engine.begin() as connection:
            row = connection.exec_driver_sql(
                "SELECT partition_by FROM _partition_spec WHERE table_name = ?",
                (physical,),
            ).fetchone()
        if row is None or row[0] is None or row[0] == "":
            return []
        return row[0].split(",")

    # -- the write -------------------------------------------------------

    def load(self, *, dataset, tables, namespace, tenant=None, pipeline_name=None):
        self.load_calls.append(
            {
                "dataset": dataset,
                "tables": [dict(t) for t in tables],
                "namespace": namespace,
                "tenant": tenant,
                "pipeline_name": pipeline_name,
            }
        )
        created = []
        with self._engine.begin() as connection:
            for spec in tables:
                physical = self._physical_name(spec["name"], namespace)
                connection.exec_driver_sql(
                    f'CREATE TABLE IF NOT EXISTS "{physical}" '
                    f"({spec['schema_sql']})"
                )
                partition_by = spec.get("partition_by")
                if isinstance(partition_by, str):
                    partition_by = [partition_by]
                connection.exec_driver_sql(
                    "INSERT OR REPLACE INTO _partition_spec "
                    "(table_name, partition_by) VALUES (?, ?)",
                    (physical, ",".join(partition_by or [])),
                )
                created.append(spec["name"])
        return {"created_or_appended": created}

    # -- test-side inspection --------------------------------------------

    def catalog_columns(self, name, *, namespace):
        """Read one created table's columns back OUT OF THE DATABASE."""
        inspector = sa.inspect(self._engine)
        return tuple(
            column["name"]
            for column in inspector.get_columns(self._physical_name(name, namespace))
        )


@pytest.fixture
def sqlite_sink(tmp_path):
    """A real on-disk SQLite engine wrapped in the sink capability."""
    engine = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / 'council_sink.db'}")
    try:
        yield _SqliteSinkCapability(engine)
    finally:
        engine.dispose()


class TestBootstrapAgainstRealSqliteSink:
    """``bootstrap()`` through its REAL default code path, DDL really executed.

    ``tables`` is left defaulted in every test here, so each run goes
    through ``table_configs()`` exactly as a production call would.
    """

    def test_a_first_run_creates_all_nine_tables_with_the_declared_columns(
        self, sqlite_sink
    ):
        result = bootstrap(sqlite_sink, namespace=NAMESPACE)

        assert result["dry_run"] is False
        assert [report["name"] for report in result["tables"]] == list(ARCHIVED_TABLES)
        assert all(report["action"] == "create" for report in result["tables"])

        # The assertion that matters: read the nine tables back out of the
        # real catalog, not out of the config dict.
        for table in ARCHIVED_TABLES:
            assert sqlite_sink.table_exists(table, namespace=NAMESPACE), table
            assert (
                sqlite_sink.catalog_columns(table, namespace=NAMESPACE)
                == column_names(table)
            ), table

    def test_the_created_tables_accept_a_row_shaped_like_the_declarations(
        self, sqlite_sink
    ):
        bootstrap(sqlite_sink, namespace=NAMESPACE)
        names = column_names("calls")
        with sqlite_sink._engine.begin() as connection:
            connection.exec_driver_sql(
                f'INSERT INTO "{NAMESPACE}__calls" '
                f"({', '.join(names)}) VALUES "
                f"({', '.join('?' * len(names))})",
                tuple(None for _ in names),
            )
            count = connection.exec_driver_sql(
                f'SELECT COUNT(*) FROM "{NAMESPACE}__calls"'
            ).fetchone()[0]
        assert count == 1

    def test_bootstrap_is_idempotent(self, sqlite_sink):
        first = bootstrap(sqlite_sink, namespace=NAMESPACE)
        second = bootstrap(sqlite_sink, namespace=NAMESPACE)
        third = bootstrap(sqlite_sink, namespace=NAMESPACE)

        assert all(report["action"] == "create" for report in first["tables"])
        for run in (second, third):
            assert [r["name"] for r in run["tables"]] == list(ARCHIVED_TABLES)
            assert all(report["action"] == "append" for report in run["tables"])

        # Still nine tables, still the declared columns, after three runs.
        for table in ARCHIVED_TABLES:
            assert (
                sqlite_sink.catalog_columns(table, namespace=NAMESPACE)
                == column_names(table)
            ), table

    def test_the_live_partition_spec_round_trips_through_the_sink(self, sqlite_sink):
        bootstrap(sqlite_sink, namespace=NAMESPACE)
        for table in ARCHIVED_TABLES:
            spec = sqlite_sink.table_partition_spec(table, namespace=NAMESPACE)
            expected = [PARTITION_COLUMN] if table in _PARTITIONED_TABLES else []
            assert spec == expected, table

    def test_bootstrap_never_requests_overwrite(self, sqlite_sink):
        bootstrap(sqlite_sink, namespace=NAMESPACE)
        bootstrap(sqlite_sink, namespace=NAMESPACE)
        for call in sqlite_sink.load_calls:
            for spec in call["tables"]:
                assert spec["mode"] == "append"

    def test_bootstrap_forwards_the_council_dataset_and_tenant_defaults(
        self, sqlite_sink
    ):
        bootstrap(sqlite_sink, namespace=NAMESPACE)
        assert sqlite_sink.load_calls[0]["dataset"] == NAMESPACE
        assert sqlite_sink.load_calls[0]["tenant"] == TENANT

    def test_dry_run_writes_nothing(self, sqlite_sink):
        result = bootstrap(sqlite_sink, namespace=NAMESPACE, dry_run=True)
        assert result["dry_run"] is True
        assert result["load_result"] is None
        assert sqlite_sink.load_calls == []
        for table in ARCHIVED_TABLES:
            assert not sqlite_sink.table_exists(table, namespace=NAMESPACE)

    def test_a_mismatched_live_partition_spec_refuses_and_writes_nothing(
        self, sqlite_sink
    ):
        bootstrap(sqlite_sink, namespace=NAMESPACE)
        # Rewrite calls' recorded spec to something this config does not ask
        # for, exactly as a table created under an earlier config would read.
        with sqlite_sink._engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE _partition_spec SET partition_by = ? WHERE table_name = ?",
                ("query_version", f"{NAMESPACE}__calls"),
            )
        before = len(sqlite_sink.load_calls)
        with pytest.raises(BootstrapPartitionSpecMismatchError) as excinfo:
            bootstrap(sqlite_sink, namespace=NAMESPACE)
        message = str(excinfo.value)
        assert "query_version" in message
        assert PARTITION_COLUMN in message
        assert len(sqlite_sink.load_calls) == before

    def test_an_empty_namespace_is_refused(self, sqlite_sink):
        with pytest.raises(ValueError, match="namespace"):
            bootstrap(sqlite_sink, namespace="")
        assert sqlite_sink.load_calls == []

    def test_an_indeterminate_existence_probe_refuses_rather_than_overwriting(
        self, sqlite_sink
    ):
        class _Broken(_SqliteSinkCapability):
            def table_exists(self, name, *, namespace):
                raise RuntimeError("catalog unreachable")

        broken = _Broken(sqlite_sink._engine)
        with pytest.raises(BootstrapIndeterminateStateError):
            bootstrap(broken, namespace=NAMESPACE)
        assert broken.load_calls == []
