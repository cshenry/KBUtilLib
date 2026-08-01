"""Unit tests for kbutillib.domains.kbase.berdl.naming.

Pure logic, no network: dotted/underscored database-name pairing and
dedup, legacy marking, and my/{username} personal-catalog alias
translation in both directions.
"""

from kbutillib.domains.kbase.berdl.naming import (
    NormalizedDatabase,
    normalize_databases,
    to_spark_alias,
    to_trino_alias,
)


class TestNormalizeDatabases:
    """dotted/underscored pairing, dedup, and legacy marking."""

    def test_pairs_dotted_and_underscored_into_one_entry(self):
        """A dotted name and its underscored counterpart dedupe to one entry."""
        result = normalize_databases(["aiale.dataset1", "aiale_dataset1"])

        assert len(result) == 1
        entry = result[0]
        assert entry.name == "aiale.dataset1"
        assert entry.is_iceberg is True
        assert entry.legacy_alias == "aiale_dataset1"

    def test_prefers_dotted_form_regardless_of_input_order(self):
        """The dotted form wins as the canonical name whichever order they arrive in."""
        forward = normalize_databases(["aiale.dataset1", "aiale_dataset1"])
        reverse = normalize_databases(["aiale_dataset1", "aiale.dataset1"])

        assert forward[0].name == "aiale.dataset1"
        assert reverse[0].name == "aiale.dataset1"

    def test_underscored_form_not_hidden_but_marked_legacy(self):
        """The underscored counterpart is retained on the entry, not dropped."""
        result = normalize_databases(
            ["kbaseincubator.fitness", "kbaseincubator_fitness"]
        )

        entry = result[0]
        assert entry.legacy_alias == "kbaseincubator_fitness"
        # It must not show up as a second, unrelated top-level entry.
        assert len(result) == 1

    def test_two_logical_datasets_stay_distinct(self):
        """Different logical datasets are never merged with one another."""
        result = normalize_databases(
            [
                "aiale.dataset1",
                "aiale_dataset1",
                "kbaseincubator.fitness",
                "kbaseincubator_fitness",
            ]
        )

        assert len(result) == 2
        names = {entry.name for entry in result}
        assert names == {"aiale.dataset1", "kbaseincubator.fitness"}

    def test_dotted_only_no_underscored_counterpart(self):
        """A dotted name with no underscored sibling is still Iceberg, no legacy alias."""
        result = normalize_databases(["aiale.dataset1"])

        entry = result[0]
        assert entry == NormalizedDatabase(
            name="aiale.dataset1", is_iceberg=True, legacy_alias=None
        )

    def test_underscored_only_marked_non_iceberg(self):
        """An underscored name with no dotted sibling is surfaced as legacy, not hidden."""
        result = normalize_databases(["aiale_dataset1"])

        entry = result[0]
        assert entry.name == "aiale_dataset1"
        assert entry.is_iceberg is False
        assert entry.legacy_alias is None

    def test_preserves_first_seen_order(self):
        """Output order follows first appearance of each logical dataset."""
        result = normalize_databases(
            [
                "kbaseincubator_fitness",
                "aiale.dataset1",
                "kbaseincubator.fitness",
                "aiale_dataset1",
            ]
        )

        assert [entry.name for entry in result] == [
            "kbaseincubator.fitness",
            "aiale.dataset1",
        ]

    def test_empty_input(self):
        """No names in, no entries out."""
        assert normalize_databases([]) == []


class TestPersonalCatalogAlias:
    """`my` (Spark) <-> `{username}` (Trino) translation, both directions."""

    def test_spark_bare_alias_to_trino(self):
        assert to_trino_alias("my", "chenry") == "chenry"

    def test_spark_qualified_name_to_trino(self):
        assert to_trino_alias("my.dataset1", "chenry") == "chenry.dataset1"

    def test_trino_bare_username_to_spark(self):
        assert to_spark_alias("chenry", "chenry") == "my"

    def test_trino_qualified_name_to_spark(self):
        assert to_spark_alias("chenry.dataset1", "chenry") == "my.dataset1"

    def test_round_trip_spark_to_trino_to_spark(self):
        trino_name = to_trino_alias("my.dataset1", "chenry")
        assert to_spark_alias(trino_name, "chenry") == "my.dataset1"

    def test_round_trip_trino_to_spark_to_trino(self):
        spark_name = to_spark_alias("chenry.dataset1", "chenry")
        assert to_trino_alias(spark_name, "chenry") == "chenry.dataset1"

    def test_non_personal_name_untouched_by_to_trino(self):
        """A tenant name that isn't the personal alias passes through unchanged."""
        assert to_trino_alias("aiale.dataset1", "chenry") == "aiale.dataset1"

    def test_non_personal_name_untouched_by_to_spark(self):
        """A tenant name that isn't the caller's own username passes through unchanged."""
        assert to_spark_alias("aiale.dataset1", "chenry") == "aiale.dataset1"

    def test_other_users_name_not_mistaken_for_personal_catalog(self):
        """Another user's qualified name is not rewritten to `my` for this caller."""
        assert (
            to_spark_alias("someone_else.dataset1", "chenry") == "someone_else.dataset1"
        )
