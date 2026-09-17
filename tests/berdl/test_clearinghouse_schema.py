"""Unit tests for kbutillib.domains.kbase.berdl.clearinghouse_schema.

Pure logic, no network, no live BERDL pod: table config shape for
``BerdlCapability.load`` (the fifteen ``<entity_type>_<kind>`` tables), and
the hex/binary ``entity_hash`` encoding round-trip. Per the module
docstring, the encode/decode helpers exist to bridge
:mod:`kbutillib.domains.identity.standardizers` (hex digests) to this
schema's binary ``entity_hash`` column -- the hex-vs-binary non-equality
test below is a regression test for exactly that gap, not a redundant one,
and must not be removed as trivial.
"""

import re

import pytest

from kbutillib.domains.identity import standardizers
from kbutillib.domains.kbase.berdl.clearinghouse_parity_fixture import ALL_PARITY_ROWS
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    ENTITY_TYPES,
    NAMESPACE,
    TENANT,
    decode_entity_hash,
    encode_entity_hash,
    table_configs,
    table_name,
)

#: Columns every ``<type>_entity`` table declares (GENERIC across types).
_ENTITY_COLUMNS = {
    "entity_hash": "BINARY",
    "entity_type": "STRING",
    "standardizer_version": "STRING",
    "observed_at": "TIMESTAMP",
    "ingest_batch_id": "STRING",
}

#: Columns every ``<type>_result`` table declares (GENERIC across types).
_RESULT_COLUMNS = {
    "entity_hash": "BINARY",
    "entity_type": "STRING",
    "result_type": "STRING",
    "source": "STRING",
    "result_type_version": "STRING",
    "payload": "STRING",
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
    def test_returns_exactly_fifteen_configs_with_the_expected_names(self):
        configs = table_configs()
        names = [table["name"] for table in configs]
        expected = (
            [f"{t}_entity" for t in ENTITY_TYPES]
            + [f"{t}_content" for t in ENTITY_TYPES]
            + [f"{t}_result" for t in ENTITY_TYPES]
        )
        # Documented order: all five entity, then all five content, then all
        # five result, each in ENTITY_TYPES order.
        assert names == expected
        assert len(configs) == 15
        assert len(set(names)) == 15

    def test_every_name_equals_table_name_resolver(self):
        # Generator and resolver must not drift: each config's name is
        # exactly table_name(entity_type, kind) for its type and kind.
        expected = set()
        for kind in ("entity", "content", "result"):
            for entity_type in ENTITY_TYPES:
                expected.add(table_name(entity_type, kind))
        actual = {table["name"] for table in table_configs()}
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
            assert cols["entity_hash"] == "BINARY"
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
        assert gene["protein_entity_hash"] == "BINARY"

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

    def test_the_six_unpartitioned_tables_omit_the_key_entirely(self):
        configs = _configs_by_name()
        unpartitioned = {
            "genome_entity",
            "function_entity",
            "ontology_term_entity",
            "genome_content",
            "function_content",
            "ontology_term_content",
        }
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


class TestEntityHashRoundTrip:
    _HEX = "a" * 63 + "b"  # a valid 64-char hex string

    def test_hex_to_bytes_to_hex_is_identity(self):
        encoded = encode_entity_hash(self._HEX)
        assert isinstance(encoded, bytes)
        assert len(encoded) == 32
        assert decode_entity_hash(encoded) == self._HEX

    def test_uppercase_hex_input_yields_same_bytes_as_lowercase(self):
        assert encode_entity_hash(self._HEX.upper()) == encode_entity_hash(
            self._HEX.lower()
        )

    def test_decode_output_is_always_lowercase(self):
        encoded = encode_entity_hash(self._HEX.upper())
        decoded = decode_entity_hash(encoded)
        assert decoded == decoded.lower()
        assert decoded == self._HEX.lower()

    def test_bytes_input_passes_through_unchanged(self):
        raw = bytes(range(32))
        assert encode_entity_hash(raw) == raw

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
            decode_entity_hash(b"\x00" * 31)
        with pytest.raises(ValueError):
            decode_entity_hash("not bytes")


class TestHexBridgeToStandardizers:
    """Regression test: a standardizer's hex digest must never be mistaken
    for the binary column value -- see the module docstring's "gap is not
    cosmetic" paragraph. This is deliberately not redundant with the
    round-trip tests above: those confirm encode/decode agree with each
    other, this confirms the *raw hex string* is not itself usable where
    the binary encoding is required.
    """

    def test_hex_digest_is_not_equal_to_its_binary_encoding(self):
        hex_digest = standardizers.entity_hash("function", "example function")
        assert re.fullmatch(r"[0-9a-f]{64}", hex_digest)
        binary_value = encode_entity_hash(hex_digest)
        # The whole point: a hex STRING and its binary encoding must never
        # compare equal. If a caller bound the hex string directly against
        # a BINARY column, this would be the (false) equality that makes
        # every dedup probe silently return 'unknown'.
        assert hex_digest != binary_value
        assert isinstance(hex_digest, str)
        assert isinstance(binary_value, bytes)

    def test_binary_encoding_round_trips_back_to_the_standardizer_hex(self):
        hex_digest = standardizers.entity_hash("protein", "MKV*")
        binary_value = encode_entity_hash(hex_digest)
        assert decode_entity_hash(binary_value) == hex_digest


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
