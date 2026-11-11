"""Unit tests for kbutillib.domains.kbase.berdl.clearinghouse_schema.

Pure logic, no network, no live BERDL pod: table config shape for
``BerdlCapability.load`` (the fifteen ``<entity_type>_<kind>`` tables plus
the parameter-set registry), and
the hex/binary ``entity_hash`` encoding round-trip. Per the module
docstring, the encode/decode helpers exist to bridge
:mod:`kbutillib.domains.identity.standardizers` (hex digests) to this
schema's binary ``entity_hash`` column -- the hex-vs-binary non-equality
test below is a regression test for exactly that gap, not a redundant one,
and must not be removed as trivial.
"""

import re

import pytest

from kbutillib.domains.identity import DEFAULT_PARAMETER_SET_HASH, standardizers
from kbutillib.domains.kbase.berdl.clearinghouse_parity_fixture import (
    ALL_PARITY_ROWS,
    PARITY_CASES,
)
from kbutillib.domains.kbase.berdl import clearinghouse_schema as _schema_module
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    ALIAS_OBSERVATIONS,
    ALIAS_SOURCE_PATTERNS,
    ALIAS_SOURCES,
    ALIAS_SOURCES_BY_ENTITY_TYPE,
    ENTITY_TYPES,
    INVERTIBLE_ALIAS_SOURCES,
    NAMESPACE,
    PARAMETER_SET_TABLE,
    TENANT,
    column_names,
    decode_entity_hash,
    encode_entity_hash,
    parameter_set_column_names,
    parameter_set_columns,
    table_columns,
    table_configs,
    table_name,
    union_view_sql,
    validate_alias,
)

#: Columns every ``<type>_entity`` table declares (GENERIC across types).
_ENTITY_COLUMNS = {
    "entity_hash": "STRING",
    "entity_type": "STRING",
    "standardizer_version": "STRING",
    "observed_at": "TIMESTAMP",
    "ingest_batch_id": "STRING",
}

#: Columns every ``<type>_result`` table declares (GENERIC across types).
#: ``parameter_set_hash`` sits immediately after ``source``; see
#: :data:`_RESULT_COLUMN_ORDER` for the order assertion.
_RESULT_COLUMNS = {
    "entity_hash": "STRING",
    "entity_type": "STRING",
    "result_type": "STRING",
    "source": "STRING",
    "parameter_set_hash": "STRING",
    "result_type_version": "STRING",
    "payload": "STRING",
    "observed_at": "TIMESTAMP",
    "ingest_batch_id": "STRING",
}

#: The DDL ORDER of the result columns, asserted separately from the
#: name->type mapping above because ``parameter_set_hash``'s POSITION is
#: part of the contract: it must come immediately after ``source``.
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

#: Columns the parameter-set registry declares. The registry is the
#: sixteenth table and the only one that is not entity-typed.
_PARAMETER_SET_COLUMNS = {
    "parameter_set_hash": "STRING",
    "canonical_json": "STRING",
    "observed_at": "TIMESTAMP",
    "ingest_batch_id": "STRING",
}

#: The shared tail every ``<type>_content`` table carries.
_CONTENT_COMMON_TAIL = {
    "standardizer_version": "STRING",
    "observed_at": "TIMESTAMP",
    "ingest_batch_id": "STRING",
}


def _columns_from_schema_sql(schema_sql: str) -> dict[str, str]:
    """Parse a ``"name TYPE, name TYPE, ..."`` DDL fragment into a dict."""
    columns = {}
    for part in schema_sql.split(","):
        name, sql_type = part.strip().split(" ", 1)
        columns[name] = sql_type
    return columns


def _schema_sql_from_columns(columns) -> str:
    """Render ``(name, sql_type)`` pairs the way ``table_configs`` does."""
    return ", ".join(f"{name} {sql_type}" for name, sql_type in columns)


def _configs_by_name() -> dict[str, dict]:
    return {table["name"]: table for table in table_configs()}


class TestModuleConstants:
    def test_tenant_and_namespace(self):
        assert TENANT == "kbaseincubator"
        assert NAMESPACE == "clearinghouse"

    def test_entity_types_are_the_renamed_five(self):
        # The gene_dna -> gene rename (2026-09-14): table_name() builds
        # f"{entity_type}_{kind}", so gene_entity is only reachable once the
        # type itself is renamed.
        assert ENTITY_TYPES == (
            "genome",
            "protein",
            "gene",
            "function",
            "ontology_term",
        )


class TestTableName:
    def test_composes_type_and_kind(self):
        assert table_name("gene", "entity") == "gene_entity"
        assert table_name("genome", "content") == "genome_content"
        assert table_name("protein", "result") == "protein_result"

    def test_raises_on_unknown_entity_type(self):
        with pytest.raises(ValueError):
            table_name("gene_dna", "entity")
        with pytest.raises(ValueError):
            table_name("bogus", "entity")

    def test_raises_on_unknown_kind(self):
        with pytest.raises(ValueError):
            table_name("gene", "canonical_content")
        with pytest.raises(ValueError):
            table_name("gene", "bogus")


class TestTableConfigs:
    def test_returns_exactly_twenty_one_configs_with_the_expected_names(self):
        configs = table_configs()
        names = [table["name"] for table in configs]
        expected = (
            [f"{t}_entity" for t in ENTITY_TYPES]
            + [f"{t}_content" for t in ENTITY_TYPES]
            + [f"{t}_result" for t in ENTITY_TYPES]
            + [PARAMETER_SET_TABLE]
            + [f"{t}_alias" for t in ENTITY_TYPES]
        )
        # Documented order: all five entity, then all five content, then all
        # five result, each in ENTITY_TYPES order, then the parameter-set
        # registry, then all five alias tables last.
        assert names == expected
        assert len(configs) == 21
        assert len(set(names)) == 21

    def test_every_per_type_name_equals_table_name_resolver(self):
        # Generator and resolver must not drift: each per-entity-type
        # config's name is exactly table_name(entity_type, kind) for its
        # type and kind. The registry is excluded deliberately -- it is not
        # entity-typed, so table_name() cannot and must not name it (see
        # TestParameterSetRegistry).
        expected = set()
        for kind in ("entity", "content", "result", "alias"):
            for entity_type in ENTITY_TYPES:
                expected.add(table_name(entity_type, kind))
        actual = {
            table["name"]
            for table in table_configs()
            if table["name"] != PARAMETER_SET_TABLE
        }
        assert actual == expected

    def test_entity_tables_have_the_generic_entity_columns(self):
        configs = _configs_by_name()
        for entity_type in ENTITY_TYPES:
            cols = _columns_from_schema_sql(
                configs[f"{entity_type}_entity"]["schema_sql"]
            )
            assert cols == _ENTITY_COLUMNS

    def test_result_tables_have_the_generic_result_columns(self):
        configs = _configs_by_name()
        for entity_type in ENTITY_TYPES:
            cols = _columns_from_schema_sql(
                configs[f"{entity_type}_result"]["schema_sql"]
            )
            assert cols == _RESULT_COLUMNS

    def test_result_columns_are_in_the_declared_ddl_order(self):
        """The nine result columns, in order, with parameter_set_hash
        immediately after source.

        Asserted as an ORDERED sequence, separately from the name->type
        mapping above: ``schema_sql`` is a positional DDL fragment, and the
        parity script builds its Spark rows POSITIONALLY from it, so a
        column landing in the wrong position is a real defect that a
        dict-equality assertion cannot see.
        """
        configs = _configs_by_name()
        for entity_type in ENTITY_TYPES:
            order = tuple(
                _columns_from_schema_sql(
                    configs[f"{entity_type}_result"]["schema_sql"]
                )
            )
            assert order == _RESULT_COLUMN_ORDER, entity_type
            # Stated once more as the relationship that matters, so a
            # failure reads as "the hash moved" and not just "a tuple
            # differs".
            assert order.index("parameter_set_hash") == order.index("source") + 1

    def test_result_columns_match_the_table_columns_accessor(self):
        # table_columns()/column_names() and the config generator must read
        # the same declaration.
        configs = _configs_by_name()
        for entity_type in ENTITY_TYPES:
            assert column_names(entity_type, "result") == _RESULT_COLUMN_ORDER
            assert dict(table_columns(entity_type, "result")) == _RESULT_COLUMNS
            assert (
                _schema_sql_from_columns(table_columns(entity_type, "result"))
                == configs[f"{entity_type}_result"]["schema_sql"]
            )

    def test_all_entity_schemas_are_byte_identical(self):
        # "Generic schema, split physically": every <type>_entity table's
        # schema_sql must be byte-for-byte identical -- the test most likely
        # to catch an accidental divergence.
        configs = _configs_by_name()
        schemas = {configs[f"{t}_entity"]["schema_sql"] for t in ENTITY_TYPES}
        assert len(schemas) == 1

    def test_all_result_schemas_are_byte_identical(self):
        configs = _configs_by_name()
        schemas = {configs[f"{t}_result"]["schema_sql"] for t in ENTITY_TYPES}
        assert len(schemas) == 1

    def test_content_tables_carry_the_shared_tail(self):
        configs = _configs_by_name()
        for entity_type in ENTITY_TYPES:
            cols = _columns_from_schema_sql(
                configs[f"{entity_type}_content"]["schema_sql"]
            )
            # entity_hash leads every content table.
            assert next(iter(cols)) == "entity_hash"
            assert cols["entity_hash"] == "STRING"
            for name, sql_type in _CONTENT_COMMON_TAIL.items():
                assert cols[name] == sql_type

    def test_content_type_specific_columns(self):
        configs = _configs_by_name()

        protein = _columns_from_schema_sql(
            configs["protein_content"]["schema_sql"]
        )
        assert protein["sequence"] == "STRING"
        assert protein["seq_length"] == "INT"

        gene = _columns_from_schema_sql(configs["gene_content"]["schema_sql"])
        assert gene["sequence"] == "STRING"
        assert gene["seq_length"] == "INT"
        # Nullable pointer -- not every gene codes for a protein.
        assert gene["protein_entity_hash"] == "STRING"

        function = _columns_from_schema_sql(
            configs["function_content"]["schema_sql"]
        )
        assert function["definition"] == "STRING"
        assert "name" not in function

        ontology = _columns_from_schema_sql(
            configs["ontology_term_content"]["schema_sql"]
        )
        for col in ("term_id", "name", "namespace", "definition"):
            assert ontology[col] == "STRING"

        genome = _columns_from_schema_sql(
            configs["genome_content"]["schema_sql"]
        )
        assert genome["fasta_reference"] == "STRING"
        assert genome["taxon_id"] == "STRING"
        assert genome["assembly_accession"] == "STRING"
        assert genome["n_contigs"] == "INT"
        assert genome["total_length"] == "BIGINT"
        assert genome["is_closed"] == "BOOLEAN"
        # A genome stores a POINTER to its FASTA, never the sequence inlined.
        assert "sequence" not in genome

    def test_alias_tables_are_last_and_partitioned_on_alias_source(self):
        configs = table_configs()
        for config, entity_type in zip(configs[-5:], ENTITY_TYPES):
            assert config["name"] == f"{entity_type}_alias"
            assert config["partition_by"] == "alias_source"
        partitioned = {
            name
            for name, cfg in _configs_by_name().items()
            if cfg.get("partition_by") == "alias_source"
        }
        assert partitioned == {f"{t}_alias" for t in ENTITY_TYPES}

    def test_standardizer_version_partitioning_on_exactly_four_tables(self):
        configs = _configs_by_name()
        partitioned = {
            name
            for name, cfg in configs.items()
            if cfg.get("partition_by") == "standardizer_version"
        }
        assert partitioned == {
            "gene_entity",
            "protein_entity",
            "gene_content",
            "protein_content",
        }

    def test_source_partitioning_on_exactly_the_five_result_tables(self):
        configs = _configs_by_name()
        partitioned = {
            name
            for name, cfg in configs.items()
            if cfg.get("partition_by") == "source"
        }
        assert partitioned == {f"{t}_result" for t in ENTITY_TYPES}

    def test_source_is_the_only_partition_column_of_the_result_tables(self):
        """``parameter_set_hash`` joined the result schema but NOT the
        partition spec.

        It is a hash, so it falls under the same rule as ``entity_hash``: a
        good hash scatters uniformly, so partitioning on it gives zero read
        pruning while multiplying partition count. ``source`` stays the one
        partition column.
        """
        configs = _configs_by_name()
        for entity_type in ENTITY_TYPES:
            assert configs[f"{entity_type}_result"]["partition_by"] == "source"

    def test_parameter_set_hash_is_not_a_partition_key_on_any_table(self):
        for config in table_configs():
            partition_by = config.get("partition_by")
            if partition_by is None:
                continue
            columns = (
                [partition_by] if isinstance(partition_by, str) else list(partition_by)
            )
            assert "parameter_set_hash" not in columns, config["name"]

    def test_the_unpartitioned_tables_omit_the_key_entirely(self):
        configs = _configs_by_name()
        unpartitioned = {
            "genome_entity",
            "function_entity",
            "ontology_term_entity",
            "genome_content",
            "function_content",
            "ontology_term_content",
            # The parameter-set registry: one row per DISTINCT parameter set
            # ever used, so a partition key buys nothing.
            PARAMETER_SET_TABLE,
        }
        # Exhaustive: these are ALL of them, so a table silently losing its
        # partition key fails here too.
        assert {
            name for name, cfg in configs.items() if "partition_by" not in cfg
        } == unpartitioned
        for name in unpartitioned:
            # Assert key ABSENCE, not a falsy value: an absent key is the
            # unambiguous signal for "unpartitioned".
            assert "partition_by" not in configs[name], name

    def test_no_config_value_anywhere_contains_a_parenthesis(self):
        """Guards against an Iceberg partition-transform expression (e.g.
        ``bucket(256, entity_hash)``) ever leaking into a config value --
        the wrapper reads ``partition_by`` entries as bare PySpark
        ``partitionedBy`` column names, so a transform expression would be
        misread as a nonexistent column and fail at runtime.
        """

        def _walk(value):
            if isinstance(value, str):
                assert "(" not in value and ")" not in value, value
            elif isinstance(value, dict):
                for v in value.values():
                    _walk(v)
            elif isinstance(value, (list, tuple)):
                for v in value:
                    _walk(v)

        for table in table_configs():
            _walk(table)

    def test_table_configs_returns_a_fresh_list_each_call(self):
        """Callers may mutate the result without corrupting module state."""
        first = table_configs()
        first[0]["name"] = "mutated"
        second = table_configs()
        assert second[0]["name"] == "genome_entity"


class TestParameterSetRegistry:
    """The sixteenth table: the parameter-set registry.

    It exists so each distinct parameter set is stored ONCE -- with its
    canonical-JSON pre-image, so the hash rule stays recomputable -- rather
    than repeating the parameter text on every result row. It is the only
    clearinghouse table that is NOT entity-typed, and that distinction is
    load-bearing: it must not appear in ENTITY_TYPES, must not be built by
    table_name(), and must not be unioned into any all_<kind> view, because
    all three would put a parameter-set lookup table into the per-entity
    query surface.
    """

    def test_registry_is_the_only_non_entity_typed_table(self):
        for config in table_configs():
            if config["name"] == PARAMETER_SET_TABLE:
                continue
            etype, _, kind = config["name"].partition("_")
            # type-first names: every other table resolves through table_name().
            assert any(
                config["name"] == table_name(t, k)
                for t in ENTITY_TYPES
                for k in ("entity", "content", "result", "alias")
            ), config["name"]

    def test_table_name_constant_is_parameter_set(self):
        assert PARAMETER_SET_TABLE == "parameter_set"

    def test_registry_config_is_present_and_precedes_the_alias_tables(self):
        # Order: 15 entity/content/result, the registry, then 5 alias tables.
        configs = table_configs()
        assert configs[15]["name"] == PARAMETER_SET_TABLE

    def test_registry_declares_exactly_the_four_columns_in_order(self):
        config = _configs_by_name()[PARAMETER_SET_TABLE]
        cols = _columns_from_schema_sql(config["schema_sql"])
        assert cols == _PARAMETER_SET_COLUMNS
        assert tuple(cols) == (
            "parameter_set_hash",
            "canonical_json",
            "observed_at",
            "ingest_batch_id",
        )

    def test_registry_is_unpartitioned_by_key_absence(self):
        # Key ABSENCE, not a falsy value -- the same convention the six
        # unpartitioned per-type tables follow.
        config = _configs_by_name()[PARAMETER_SET_TABLE]
        assert "partition_by" not in config

    def test_registry_schema_sql_is_rendered_from_the_accessor(self):
        config = _configs_by_name()[PARAMETER_SET_TABLE]
        assert (
            _schema_sql_from_columns(parameter_set_columns())
            == config["schema_sql"]
        )
        assert parameter_set_column_names() == tuple(_PARAMETER_SET_COLUMNS)

    def test_registry_is_not_an_entity_type(self):
        assert PARAMETER_SET_TABLE not in ENTITY_TYPES
        # And table_name() refuses to name it, in either argument position.
        with pytest.raises(ValueError):
            table_name(PARAMETER_SET_TABLE, "entity")
        with pytest.raises(ValueError):
            table_name("protein", PARAMETER_SET_TABLE)

    def test_registry_appears_in_no_union_view(self):
        for kind in ("all_entity", "all_content", "all_result", "all_alias"):
            sql = union_view_sql(kind, fqn_prefix=f"{TENANT}.{NAMESPACE}")
            assert PARAMETER_SET_TABLE not in sql, kind
            assert "canonical_json" not in sql, kind

    def test_registry_has_no_entity_hash_column(self):
        # It is keyed by parameter_set_hash, not by an entity. A regression
        # that gave it an entity_hash would mean it had been modelled as a
        # per-entity table.
        cols = _columns_from_schema_sql(
            _configs_by_name()[PARAMETER_SET_TABLE]["schema_sql"]
        )
        assert "entity_hash" not in cols

    def test_every_result_parameter_set_hash_has_a_registry_home(self):
        """The registry's hash column matches the result tables' by name.

        The orphan check an operator runs (distinct result
        ``parameter_set_hash`` values absent from the registry) only works
        if both tables spell the column the same way.
        """
        registry = _columns_from_schema_sql(
            _configs_by_name()[PARAMETER_SET_TABLE]["schema_sql"]
        )
        for entity_type in ENTITY_TYPES:
            result = _columns_from_schema_sql(
                _configs_by_name()[f"{entity_type}_result"]["schema_sql"]
            )
            assert "parameter_set_hash" in result
            assert (
                registry["parameter_set_hash"] == result["parameter_set_hash"]
            )


class TestEntityHashRoundTrip:
    _HEX = "a" * 63 + "b"  # a valid 64-char hex string

    def test_encode_returns_the_canonical_hex_string(self):
        encoded = encode_entity_hash(self._HEX)
        assert isinstance(encoded, str)
        assert len(encoded) == 64
        assert decode_entity_hash(encoded) == self._HEX

    def test_uppercase_hex_input_yields_the_same_value_as_lowercase(self):
        """STRING equality is case-sensitive, so this is the load-bearing
        normalisation: an uppercase digest must bind as the stored form.
        """
        assert encode_entity_hash(self._HEX.upper()) == self._HEX.lower()

    def test_decode_output_is_always_lowercase(self):
        decoded = decode_entity_hash(self._HEX.upper())
        assert decoded == self._HEX.lower()

    def test_raw_bytes_input_encodes_to_hex(self):
        raw = bytes(range(32))
        assert encode_entity_hash(raw) == raw.hex()

    @pytest.mark.parametrize(
        "bad_value",
        [
            "a" * 63,  # 63-char hex: too short
            "a" * 65,  # 65-char hex: too long
            b"\x00" * 31,  # 31 raw bytes: too short
            b"\x00" * 33,  # 33 raw bytes: too long
            "g" * 64,  # 64 chars but not valid hex
        ],
    )
    def test_encode_rejects_malformed_input(self, bad_value):
        with pytest.raises(ValueError):
            encode_entity_hash(bad_value)

    def test_encode_rejects_wrong_type(self):
        with pytest.raises(ValueError):
            encode_entity_hash(12345)

    def test_decode_rejects_wrong_length_or_type(self):
        with pytest.raises(ValueError):
            decode_entity_hash("a" * 63)
        with pytest.raises(ValueError):
            decode_entity_hash("g" * 64)

    def test_decode_refuses_bytes_from_the_retired_binary_schema(self):
        """A bytes value read from this column means a row was written
        under the retired BINARY schema -- fail rather than convert it.
        """
        with pytest.raises(ValueError):
            decode_entity_hash(bytes(range(32)))


class TestNoBinaryColumnSurvives:
    """Guards the 2026-09-21 reversal (dev 1219).

    data_lakehouse_ingest has no BINARY in its schema_sql type map, so a
    BINARY column makes every sanctioned write into that table fail. A
    Spark CREATE succeeding proves nothing here -- OP2 created BINARY
    tables that then accepted zero rows.
    """

    def test_no_table_config_declares_a_binary_column(self):
        for config in table_configs():
            assert "BINARY" not in config["schema_sql"].upper(), config["name"]


class TestHexBridgeToStandardizers:
    """The stored form IS the standardizer's own output.

    The column holds the lowercase hex digest the standardizers emit, so a
    standardizer hash passes through the encode seam unchanged. Were that
    ever to stop being true -- a standardizer emitting uppercase, or the
    column changing form again -- every dedup probe would silently match
    nothing, which is the failure these tests exist to catch.
    """

    def test_standardizer_digest_is_already_the_stored_form(self):
        hex_digest = standardizers.entity_hash("function", "example function")
        assert re.fullmatch(r"[0-9a-f]{64}", hex_digest)
        assert encode_entity_hash(hex_digest) == hex_digest

    def test_standardizer_digest_round_trips(self):
        hex_digest = standardizers.entity_hash("protein", "MKV*")
        assert decode_entity_hash(encode_entity_hash(hex_digest)) == hex_digest


class TestParityFixtureMatchesResultSchema:
    """Regression test for task 914: ``scripts/clearinghouse_parity_check.py``
    builds its Spark rows POSITIONALLY from the ``result`` table's declared
    column list, looking each column up by name in the fixture dicts. That is
    only safe while every fixture row supplies exactly the declared columns,
    which is the invariant asserted here.

    It is asserted in this file, off-pod and without ``pyspark``, because the
    failure it guards is otherwise invisible until an operator runs OP3
    against the live table: when ``entity_type`` joined the slot key, the
    parity script's hand-maintained ``Row(field=...)`` keyword list was not
    updated, so the schema declared eight non-nullable fields while the rows
    supplied seven. Do not remove this as redundant with the DuckDB
    derivation tests -- those insert positionally from a hand-written
    ``INSERT`` statement and so cannot detect a fixture/schema divergence.

    The result schema is GENERIC (byte-identical across the five
    ``<type>_result`` tables), so any one of them defines the declared
    columns the parity rows must supply.
    """

    def _result_column_names(self):
        configs = _configs_by_name()
        return list(
            _columns_from_schema_sql(configs["protein_result"]["schema_sql"])
        )

    def test_every_fixture_row_supplies_exactly_the_declared_result_columns(self):
        declared = set(self._result_column_names())
        assert declared, "result schema declared no columns"
        for index, row in enumerate(ALL_PARITY_ROWS):
            assert set(row) == declared, (
                f"ALL_PARITY_ROWS[{index}] does not match the declared result "
                f"columns: missing {sorted(declared - set(row))!r}, "
                f"unexpected {sorted(set(row) - declared)!r}"
            )

    def test_entity_type_is_declared_and_supplied_by_every_fixture_row(self):
        # Named separately from the set-equality test above so a future
        # regression on this specific column fails with an obvious name.
        assert "entity_type" in self._result_column_names()
        assert all("entity_type" in row for row in ALL_PARITY_ROWS)

    def test_parameter_set_hash_is_declared_and_supplied_by_every_fixture_row(
        self,
    ):
        # Same reasoning as entity_type above: named separately so a
        # regression on this specific column fails with an obvious name.
        assert "parameter_set_hash" in self._result_column_names()
        for index, row in enumerate(ALL_PARITY_ROWS):
            value = row.get("parameter_set_hash")
            assert re.fullmatch(r"[0-9a-f]{64}", value or ""), (
                f"ALL_PARITY_ROWS[{index}] carries a malformed "
                f"parameter_set_hash {value!r}"
            )

    def test_the_six_original_properties_are_all_default_runs(self):
        """Every row of the six pre-parameter-set properties carries
        DEFAULT_PARAMETER_SET_HASH -- the hash of the empty parameter set.

        Asserted against the IMPORTED constant, never a hardcoded digest,
        so the fixture and the rule that produces it cannot drift.
        """
        for case in PARITY_CASES:
            if case.property_key == "parameter_set_forks_slot":
                continue
            assert case.parameter_set_hashes == (DEFAULT_PARAMETER_SET_HASH,), (
                f"{case.property_key} should be a pure default run, got "
                f"{case.parameter_set_hashes!r}"
            )

    def test_the_parameter_set_property_carries_two_distinct_hashes(self):
        """Guards the seventh property against going vacuous.

        If both its rows ended up on one hash they would share a slot and
        the property -- "two parameter sets are both current" -- could not
        fail even if parameter_set_hash left the slot key entirely.
        """
        case = next(
            c for c in PARITY_CASES if c.property_key == "parameter_set_forks_slot"
        )
        assert len(case.parameter_set_hashes) == 2
        assert len(set(case.parameter_set_hashes)) == 2
        assert DEFAULT_PARAMETER_SET_HASH in case.parameter_set_hashes
        # The two rows differ ONLY in parameter_set_hash, which is what
        # makes them a slot-key test rather than a filter test.
        for column in ("entity_hash", "entity_type", "result_type", "source"):
            assert len({row[column] for row in case.rows}) == 1, column


class TestUnionViewSql:
    """The three cross-type UNION ALL views (all_entity/all_content/all_result).

    After the fifteen-table split, the cross-type query surface is restored
    by three ``UNION ALL`` views. These tests assert, off-pod on SQL TEXT
    only, that each view names all five of its per-type tables; that
    ``all_content`` -- whose five tables have deliberately divergent
    columns -- unions ONLY the four shared columns plus an ``entity_type``
    discriminator and reconciles none of the type-specialized content
    columns; and that an unknown kind raises ``ValueError``.
    """

    _FQN_PREFIX = f"{TENANT}.{NAMESPACE}"

    #: The four columns common to all five ``<type>_content`` tables.
    _CONTENT_COMMON_COLUMNS = (
        "entity_hash",
        "standardizer_version",
        "observed_at",
        "ingest_batch_id",
    )

    #: Type-specialized content columns that must NOT appear in all_content.
    _CONTENT_SPECIALIZED_COLUMNS = (
        "sequence",
        "seq_length",
        "protein_entity_hash",
        "definition",
        "term_id",
        "name",
        "namespace",
        "fasta_reference",
        "taxon_id",
        "assembly_accession",
        "n_contigs",
        "total_length",
        "is_closed",
    )

    def _sql(self, kind):
        return union_view_sql(kind, fqn_prefix=self._FQN_PREFIX)

    def test_all_entity_names_all_five_entity_tables(self):
        sql = self._sql("all_entity")
        for entity_type in ENTITY_TYPES:
            assert table_name(entity_type, "entity") in sql

    def test_all_content_names_all_five_content_tables(self):
        sql = self._sql("all_content")
        for entity_type in ENTITY_TYPES:
            assert table_name(entity_type, "content") in sql

    def test_all_result_names_all_five_result_tables(self):
        sql = self._sql("all_result")
        for entity_type in ENTITY_TYPES:
            assert table_name(entity_type, "result") in sql

    def test_each_view_has_five_union_branches(self):
        for kind in ("all_entity", "all_content", "all_result", "all_alias"):
            sql = self._sql(kind)
            # Five per-type tables joined by four UNION ALL separators.
            assert sql.count("UNION ALL") == len(ENTITY_TYPES) - 1

    def test_view_ddl_is_create_or_replace_view(self):
        for kind in ("all_entity", "all_content", "all_result", "all_alias"):
            sql = self._sql(kind)
            assert "CREATE OR REPLACE VIEW" in sql
            # The view name itself is backtick-quoted, per _quote_fqn.
            assert f"`{kind}`" in sql

    def test_all_content_selects_only_common_columns_plus_discriminator(self):
        sql = self._sql("all_content")
        # Every one of the four shared columns is selected.
        for col in self._CONTENT_COMMON_COLUMNS:
            assert f"`{col}`" in sql
        # A literal entity_type discriminator is projected for each branch.
        assert "AS `entity_type`" in sql
        for entity_type in ENTITY_TYPES:
            assert f"'{entity_type}' AS `entity_type`" in sql

    def test_all_content_references_no_specialized_content_columns(self):
        sql = self._sql("all_content")
        for col in self._CONTENT_SPECIALIZED_COLUMNS:
            assert col not in sql, (
                f"all_content must not reference the type-specialized "
                f"content column {col!r}; it can only union the shared subset."
            )

    def test_all_content_does_not_use_star(self):
        # A `SELECT *` across divergent content schemas is exactly the
        # failure mode this view exists to avoid.
        assert "SELECT *" not in self._sql("all_content")

    def test_fqn_prefix_is_a_parameter_not_hardcoded(self):
        # Passing a different prefix must flow through unchanged (and
        # backtick-quoted), proving the prefix is not hardcoded.
        sql = union_view_sql("all_entity", fqn_prefix="my_tenant.my_ns")
        assert "`my_tenant`.`my_ns`" in sql

    def test_unknown_kind_raises_value_error(self):
        with pytest.raises(ValueError):
            union_view_sql("all_bogus", fqn_prefix=self._FQN_PREFIX)

    def test_bare_kind_without_all_prefix_raises_value_error(self):
        # "entity" is a table kind, not a view kind -- must be rejected.
        with pytest.raises(ValueError):
            union_view_sql("entity", fqn_prefix=self._FQN_PREFIX)


_FQN = f"{TENANT}.{NAMESPACE}"
_HASH = "ab" * 32

#: The alias DDL, in order: entity_hash FIRST, entity_type SECOND.
_ALIAS_COLUMN_ORDER = (
    "entity_hash",
    "entity_type",
    "alias_source",
    "alias_id",
    "observation",
    "observed_at",
    "ingest_batch_id",
)


class TestAliasKind:
    def test_kinds_and_view_kinds(self):
        assert _schema_module._KINDS == ("entity", "content", "result", "alias")
        assert _schema_module._VIEW_KINDS == (
            "all_entity",
            "all_content",
            "all_result",
            "all_alias",
        )

    def test_table_name_resolves_alias_for_all_five_types(self):
        for t in ENTITY_TYPES:
            assert table_name(t, "alias") == f"{t}_alias"

    def test_table_columns_is_generic_seven_columns_in_ddl_order(self):
        for t in ENTITY_TYPES:
            cols = table_columns(t, "alias")
            assert tuple(n for n, _ in cols) == _ALIAS_COLUMN_ORDER
            assert dict(cols)["observed_at"] == "TIMESTAMP"
            assert column_names(t, "alias") == _ALIAS_COLUMN_ORDER
            assert cols is _schema_module._ALIAS_COLUMNS
        names = _ALIAS_COLUMN_ORDER
        assert names.index("observation") == names.index("alias_id") + 1
        assert names.index("observation") + 1 == names.index("observed_at")

    def test_alias_config_schema_sql_is_rendered_from_the_columns(self):
        configs = _configs_by_name()
        for t in ENTITY_TYPES:
            assert configs[f"{t}_alias"]["schema_sql"] == _schema_sql_from_columns(
                table_columns(t, "alias")
            )

    def test_removed_round0_symbols_do_not_exist(self):
        for name in (
            "ENTITY_ALIAS_TABLE",
            "entity_alias_columns",
            "entity_alias_column_names",
        ):
            assert not hasattr(_schema_module, name), name

    def test_entity_alias_is_not_a_table_name(self):
        with pytest.raises(ValueError):
            table_name("entity_alias", "entity")
        with pytest.raises(ValueError):
            table_name("genome", "entity_alias")

    def test_all_alias_view_unions_exactly_the_five_alias_tables(self):
        sql = union_view_sql("all_alias", fqn_prefix=_FQN)
        assert "`all_alias`" in sql
        assert sql.count("UNION ALL") == 4
        for t in ENTITY_TYPES:
            assert f"`{t}_alias`" in sql
        for kind in ("entity", "content", "result"):
            for t in ENTITY_TYPES:
                assert f"`{t}_{kind}`" not in sql
        assert PARAMETER_SET_TABLE not in sql

    def test_all_alias_view_selects_entity_type(self):
        sql = union_view_sql("all_alias", fqn_prefix=_FQN)
        assert sql.count("`entity_type`") == len(ENTITY_TYPES)
        for col in _ALIAS_COLUMN_ORDER:
            assert f"`{col}`" in sql


class TestAliasVocabularies:
    def test_alias_sources_is_the_ten_in_order(self):
        assert ALIAS_SOURCES == (
            "kbdl_object",
            "kbdl_fasta_object",
            "ncbi_assembly",
            "gtdb_accession",
            "ncbi_taxon",
            "kbase_assembly_md5",
            "bvbrc_genome_md5",
            "arc_record",
            "arc_analysis",
            "kbase_ws_ref",
        )

    def test_no_kbdl_genome_id_source(self):
        # Deliberate absence: genome_id is fasta_object_id[:16], derivable.
        assert "kbdl_genome_id" not in ALIAS_SOURCES

    def test_observations_closed_vocabulary(self):
        assert ALIAS_OBSERVATIONS == ("present", "withdrawn")

    def test_patterns_have_exactly_four_entries(self):
        assert set(ALIAS_SOURCE_PATTERNS) == {
            "kbdl_fasta_object",
            "kbase_assembly_md5",
            "bvbrc_genome_md5",
            "ncbi_taxon",
        }
        assert all(hasattr(p, "match") for p in ALIAS_SOURCE_PATTERNS.values())

    def test_invertible_is_a_strict_subset_excluding_exactly_two(self):
        assert set(INVERTIBLE_ALIAS_SOURCES) < set(ALIAS_SOURCES)
        assert set(ALIAS_SOURCES) - set(INVERTIBLE_ALIAS_SOURCES) == {
            "ncbi_taxon",
            "arc_analysis",
        }
        for member in INVERTIBLE_ALIAS_SOURCES:
            assert member in ALIAS_SOURCES

    def test_sources_by_entity_type(self):
        assert set(ALIAS_SOURCES_BY_ENTITY_TYPE) == set(ENTITY_TYPES)
        assert set(ALIAS_SOURCES_BY_ENTITY_TYPE["genome"]) == set(ALIAS_SOURCES)
        assert len(ALIAS_SOURCES_BY_ENTITY_TYPE["genome"]) == 10
        agnostic = {"arc_record", "arc_analysis", "kbase_ws_ref", "ncbi_taxon"}
        for t in ("protein", "gene", "function", "ontology_term"):
            assert set(ALIAS_SOURCES_BY_ENTITY_TYPE[t]) == agnostic


class TestValidateAlias:
    def _ok(self, source="ncbi_assembly", alias_id="GCF_000001405.40", **kw):
        return validate_alias("genome", _HASH, source, alias_id, **kw)

    def test_returns_canonical_five_tuple_and_defaults_to_present(self):
        assert self._ok() == (
            "genome", _HASH, "ncbi_assembly", "GCF_000001405.40", "present"
        )

    def test_explicit_withdrawn_is_accepted(self):
        assert self._ok(observation="withdrawn")[4] == "withdrawn"

    def test_retracted_is_refused_and_message_names_both_values(self):
        with pytest.raises(ValueError) as exc:
            self._ok(observation="retracted")
        assert "present" in str(exc.value) and "withdrawn" in str(exc.value)

    def test_unknown_entity_type_refused(self):
        with pytest.raises(ValueError):
            validate_alias("bogus", _HASH, "arc_record", "x")

    def test_entity_hash_goes_through_the_64_hex_contract(self):
        with pytest.raises(ValueError):
            validate_alias("genome", "abc", "arc_record", "x")
        upper = validate_alias("genome", _HASH.upper(), "arc_record", "x")
        assert upper[1] == _HASH

    def test_misspelled_source_refused_and_names_legal_values(self):
        with pytest.raises(ValueError) as exc:
            self._ok(source="kbdl_genome")
        for source in ALIAS_SOURCES:
            assert source in str(exc.value)

    def test_kbdl_genome_id_refused(self):
        with pytest.raises(ValueError):
            self._ok(source="kbdl_genome_id")

    def test_ncbi_assembly_refused_on_protein_and_names_legal_set(self):
        with pytest.raises(ValueError) as exc:
            validate_alias("protein", _HASH, "ncbi_assembly", "GCF_1")
        assert "arc_record" in str(exc.value)
        assert "protein" in str(exc.value)

    def test_arc_record_accepted_on_protein(self):
        out = validate_alias("protein", _HASH, "arc_record", "rec-1")
        assert out[0] == "protein" and out[2] == "arc_record"

    @pytest.mark.parametrize("bad", ["a" * 63, "a" * 65, "A" * 64, "g" * 64])
    def test_fasta_object_near_misses_refused(self, bad):
        with pytest.raises(ValueError):
            self._ok(source="kbdl_fasta_object", alias_id=bad)

    def test_fasta_object_valid_accepted(self):
        assert self._ok(source="kbdl_fasta_object", alias_id="a" * 64)[3] == "a" * 64

    @pytest.mark.parametrize("source", ["kbase_assembly_md5", "bvbrc_genome_md5"])
    @pytest.mark.parametrize("bad", ["a" * 31, "a" * 33, "A" * 32])
    def test_md5_near_misses_refused(self, source, bad):
        with pytest.raises(ValueError):
            self._ok(source=source, alias_id=bad)

    @pytest.mark.parametrize("source", ["kbase_assembly_md5", "bvbrc_genome_md5"])
    def test_md5_valid_accepted(self, source):
        assert self._ok(source=source, alias_id="0f" * 16)[3] == "0f" * 16

    @pytest.mark.parametrize("bad", ["0", "01", "-1", "1.5", "abc"])
    def test_taxon_near_misses_refused(self, bad):
        with pytest.raises(ValueError):
            self._ok(source="ncbi_taxon", alias_id=bad)

    def test_taxon_valid_accepted(self):
        assert self._ok(source="ncbi_taxon", alias_id="2157")[3] == "2157"

    @pytest.mark.parametrize(
        "source", ["kbdl_object", "ncbi_assembly", "gtdb_accession", "arc_record",
                   "arc_analysis", "kbase_ws_ref"],
    )
    def test_opaque_sources_accept_a_string_no_pattern_would_match(self, source):
        weird = "!! not-hex, not-numeric \u00e9 !!"
        assert source not in ALIAS_SOURCE_PATTERNS
        assert self._ok(source=source, alias_id=weird)[3] == weird

    @pytest.mark.parametrize("bad", ["", "   ", None])
    def test_empty_blank_and_none_alias_id_refused(self, bad):
        with pytest.raises(ValueError):
            self._ok(alias_id=bad)

    def test_alias_id_round_trips_byte_identical_with_whitespace(self):
        out = self._ok(alias_id=" GCF_1 ")
        assert out[3] == " GCF_1 "
        assert len(out[3]) == 7
