"""Unit tests for kbutillib.domains.kbase.berdl.clearinghouse_schema.

Pure logic, no network, no live BERDL pod: table config shape for
``BerdlCapability.load``, and the hex/binary ``entity_hash`` encoding
round-trip. Per the module docstring, the encode/decode helpers exist to
bridge :mod:`kbutillib.domains.identity.standardizers` (hex digests) to
this schema's binary ``entity_hash`` column -- the hex-vs-binary
non-equality test below is a regression test for exactly that gap, not a
redundant one, and must not be removed as trivial.
"""

import re

import pytest

from kbutillib.domains.identity import standardizers
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    NAMESPACE,
    TENANT,
    decode_entity_hash,
    encode_entity_hash,
    table_configs,
)

_EXPECTED_COLUMNS = {
    "entity": {
        "entity_hash": "BINARY",
        "entity_type": "STRING",
        "standardizer_version": "STRING",
        "observed_at": "TIMESTAMP",
        "ingest_batch_id": "STRING",
    },
    "canonical_content": {
        "entity_hash": "BINARY",
        "content": "STRING",
        "standardizer_version": "STRING",
        "observed_at": "TIMESTAMP",
        "ingest_batch_id": "STRING",
    },
    "result": {
        "entity_hash": "BINARY",
        "result_type": "STRING",
        "source": "STRING",
        "result_type_version": "STRING",
        "payload": "STRING",
        "observed_at": "TIMESTAMP",
        "ingest_batch_id": "STRING",
    },
}


def _columns_from_schema_sql(schema_sql: str) -> dict[str, str]:
    """Parse a ``"name TYPE, name TYPE, ..."`` DDL fragment into a dict."""
    columns = {}
    for part in schema_sql.split(","):
        name, sql_type = part.strip().split(" ", 1)
        columns[name] = sql_type
    return columns


class TestModuleConstants:
    def test_tenant_and_namespace(self):
        assert TENANT == "kbaseincubator"
        assert NAMESPACE == "clearinghouse"


class TestTableConfigs:
    def test_returns_exactly_three_tables_with_expected_names(self):
        configs = table_configs()
        names = [table["name"] for table in configs]
        assert names == ["entity", "canonical_content", "result"]

    def test_each_table_has_the_exact_expected_columns_and_types(self):
        configs = {table["name"]: table for table in table_configs()}
        for name, expected_columns in _EXPECTED_COLUMNS.items():
            actual_columns = _columns_from_schema_sql(configs[name]["schema_sql"])
            assert actual_columns == expected_columns

    def test_result_declares_partition_by_source_and_only_source(self):
        configs = {table["name"]: table for table in table_configs()}
        partition_by = configs["result"]["partition_by"]
        if isinstance(partition_by, str):
            assert partition_by == "source"
        else:
            assert list(partition_by) == ["source"]

    def test_entity_and_canonical_content_declare_no_partition_by(self):
        configs = {table["name"]: table for table in table_configs()}
        assert "partition_by" not in configs["entity"]
        assert "partition_by" not in configs["canonical_content"]

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
        assert second[0]["name"] == "entity"


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
