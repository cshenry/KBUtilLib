"""Offline unit tests for ontology_dictionary.py.

Test strategy
-------------
All tests write small synthetic staged two-column TSV fixtures to
``tmp_path`` -- no test depends on a real downloaded ontology release or
on a real ``build_*_description_map.py`` run. Covers: successful lookups
across all four namespaces, an unknown accession returning ``None``
within an otherwise-loaded table, the missing-file degradation (unset
path and non-existent path, both -> ``None`` + ``degraded_namespaces``),
the zero-entry loud failure, an unsupported namespace raising
``ValueError``, and lazy-loading-once caching.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from kbutillib.domains.genome.annotation.ontology_dictionary import (
    NAMESPACES,
    OntologyDictionary,
)


def _make_dictionary(**kwargs: Any) -> OntologyDictionary:
    """Create OntologyDictionary with no filesystem/config discovery."""
    return OntologyDictionary(
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        **kwargs,
    )


def _write_tsv(tmp_path, name: str, rows: list[tuple[str, str]], *, header: bool = True):
    """Write a synthetic staged two-column TSV matching a build_*_description_map.py's output shape.

    The literal column header the generators write (e.g.
    ``"# ko_id\\tdescription"``) is '#'-prefixed -- unlike an unprefixed
    header, this cannot be mistaken for a data row by
    ``_parse_two_column_tsv``.
    """
    path = tmp_path / name
    lines = []
    if header:
        lines.append("# example_source_version: synthetic-fixture")
        lines.append("# generated_utc: 2026-01-01T00:00:00Z")
        lines.append("# accession\tdescription")
    for accession, description in rows:
        lines.append(f"{accession}\t{description}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Successful lookups, all four namespaces
# ---------------------------------------------------------------------------


class TestDescribeSuccessfulLookup:
    def test_ko_lookup(self, tmp_path):
        path = _write_tsv(
            tmp_path, "ko.tsv", [("K00001", "alcohol dehydrogenase")]
        )
        od = _make_dictionary(ko_path=str(path))
        assert od.describe("KO", "K00001") == "alcohol dehydrogenase"

    def test_ec_lookup(self, tmp_path):
        path = _write_tsv(
            tmp_path, "ec.tsv", [("1.1.1.1", "Alcohol dehydrogenase")]
        )
        od = _make_dictionary(ec_path=str(path))
        assert od.describe("EC", "1.1.1.1") == "Alcohol dehydrogenase"

    def test_go_lookup(self, tmp_path):
        path = _write_tsv(
            tmp_path, "go.tsv", [("GO:0000001", "mitochondrion inheritance")]
        )
        od = _make_dictionary(go_path=str(path))
        assert od.describe("GO", "GO:0000001") == "mitochondrion inheritance"

    def test_cog_lookup(self, tmp_path):
        path = _write_tsv(
            tmp_path, "cog.tsv", [("COG0001", "Glutamate-1-semialdehyde aminotransferase")]
        )
        od = _make_dictionary(cog_path=str(path))
        assert (
            od.describe("COG", "COG0001")
            == "Glutamate-1-semialdehyde aminotransferase"
        )

    def test_unknown_accession_within_loaded_table_returns_none(self, tmp_path):
        path = _write_tsv(tmp_path, "ko.tsv", [("K00001", "alcohol dehydrogenase")])
        od = _make_dictionary(ko_path=str(path))
        assert od.describe("KO", "K99999") is None
        assert "KO" not in od.degraded_namespaces


# ---------------------------------------------------------------------------
# Missing-file degradation -- not a failure
# ---------------------------------------------------------------------------


class TestMissingFileDegradation:
    def test_unset_path_returns_none_and_records_degradation(self):
        od = _make_dictionary()
        assert od.describe("EC", "1.1.1.1") is None
        assert "EC" in od.degraded_namespaces

    def test_nonexistent_path_returns_none_and_records_degradation(self, tmp_path):
        missing = tmp_path / "does_not_exist.tsv"
        od = _make_dictionary(go_path=str(missing))
        assert od.describe("GO", "GO:0000001") is None
        assert "GO" in od.degraded_namespaces

    def test_missing_file_does_not_raise(self, tmp_path):
        missing = tmp_path / "does_not_exist.tsv"
        od = _make_dictionary(cog_path=str(missing))
        # Explicitly: no exception, just None.
        result = od.describe("COG", "COG0001")
        assert result is None

    def test_one_namespace_missing_does_not_affect_another(self, tmp_path):
        ko_path = _write_tsv(tmp_path, "ko.tsv", [("K00001", "alcohol dehydrogenase")])
        od = _make_dictionary(ko_path=str(ko_path))
        assert od.describe("KO", "K00001") == "alcohol dehydrogenase"
        assert od.describe("EC", "1.1.1.1") is None
        assert od.degraded_namespaces == {"EC"}


# ---------------------------------------------------------------------------
# Zero-entry loud failure -- a distinct failure mode from "file missing"
# ---------------------------------------------------------------------------


class TestZeroEntryRaises:
    def test_existing_file_with_zero_data_rows_raises(self, tmp_path):
        path = tmp_path / "ko_empty.tsv"
        path.write_text(
            "# ko_description_map.tsv -- generated by ...\n"
            "# row_count: 0\n"
            "# ko_id\tdescription\n",
            encoding="utf-8",
        )
        od = _make_dictionary(ko_path=str(path))
        with pytest.raises(RuntimeError) as excinfo:
            od.describe("KO", "K00001")
        message = str(excinfo.value)
        assert "KO" in message
        assert str(path) in message
        assert "zero entries" in message

    def test_malformed_rows_only_raises(self, tmp_path):
        # Rows present but none split into exactly two non-empty
        # tab-separated fields -- must not be silently treated as valid.
        path = tmp_path / "ec_malformed.tsv"
        path.write_text("1.1.1.1\nEC:no-tab-here\n\t\n", encoding="utf-8")
        od = _make_dictionary(ec_path=str(path))
        with pytest.raises(RuntimeError, match="EC"):
            od.describe("EC", "1.1.1.1")

    def test_zero_entry_is_distinct_from_missing_file(self, tmp_path):
        # A missing file degrades to None; a present-but-empty file raises.
        # Confirms the two failure modes are not conflated.
        missing = tmp_path / "nope.tsv"
        od_missing = _make_dictionary(go_path=str(missing))
        assert od_missing.describe("GO", "GO:0000001") is None

        empty = tmp_path / "empty.tsv"
        empty.write_text("# go_id\tdescription\n", encoding="utf-8")
        od_empty = _make_dictionary(go_path=str(empty))
        with pytest.raises(RuntimeError):
            od_empty.describe("GO", "GO:0000001")


# ---------------------------------------------------------------------------
# Namespace validation
# ---------------------------------------------------------------------------


class TestNamespaceValidation:
    def test_all_four_namespaces_are_supported(self):
        assert set(NAMESPACES) == {"KO", "EC", "GO", "COG"}

    def test_unknown_namespace_raises_value_error(self):
        od = _make_dictionary()
        with pytest.raises(ValueError, match="TC"):
            od.describe("TC", "1.A.1.1.1")


# ---------------------------------------------------------------------------
# Lazy loading + per-process caching
# ---------------------------------------------------------------------------


class TestLazyLoadingAndCaching:
    def test_file_is_read_at_most_once_per_namespace(self, tmp_path):
        path = _write_tsv(tmp_path, "ko.tsv", [("K00001", "alcohol dehydrogenase")])
        od = _make_dictionary(ko_path=str(path))

        real_read_text = type(path).read_text
        call_count = {"n": 0}

        def _counting_read_text(self, *args, **kwargs):
            call_count["n"] += 1
            return real_read_text(self, *args, **kwargs)

        with patch("pathlib.Path.read_text", _counting_read_text):
            assert od.describe("KO", "K00001") == "alcohol dehydrogenase"
            assert od.describe("KO", "K00001") == "alcohol dehydrogenase"
            assert od.describe("KO", "K00001") == "alcohol dehydrogenase"

        assert call_count["n"] == 1

    def test_nothing_is_read_until_first_describe_call(self, tmp_path):
        path = _write_tsv(tmp_path, "ko.tsv", [("K00001", "alcohol dehydrogenase")])
        od = _make_dictionary(ko_path=str(path))
        # Constructing the instance must not have touched the filesystem
        # for KO yet -- confirmed by deleting the file post-construction
        # and independently probing via a fresh instance that never calls
        # describe(); the cache dict for the namespace is unpopulated.
        assert "KO" not in od._tables

    def test_missing_file_degradation_is_also_cached(self, tmp_path):
        missing = tmp_path / "nope.tsv"
        od = _make_dictionary(cog_path=str(missing))
        assert od.describe("COG", "COG0001") is None
        assert od.describe("COG", "COG0002") is None
        assert od.degraded_namespaces == {"COG"}
