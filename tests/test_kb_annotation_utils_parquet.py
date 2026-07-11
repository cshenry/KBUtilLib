"""Unit tests for KBAnnotationUtils' parquet-backed reaction-mapping translation path.

Covers M1 of the gaa-mapping-model-loop PRD:
``load_reaction_mapping_parquet`` + ``translate_function_to_reactions``. This is an
additive sibling to ``translate_term_to_modelseed`` / ``get_alias_hash`` /
``translate_rast_function_to_sso`` — those are exercised (and must remain unchanged)
by ``tests/test_composition_smoke.py::TestKBAnnotationUtils``.
"""

import pandas as pd
import pytest


@pytest.fixture
def annotation():
    """A KBAnnotationUtils instance backed by real cb_annotation_ontology_api data.

    Skips (rather than fails) if the sibling data repo isn't available locally,
    matching the existing convention in tests/test_composition_smoke.py.
    """
    from kbutillib.kb_annotation_utils import KBAnnotationUtils

    try:
        return KBAnnotationUtils(
            config_file=False,
            token_file=None,
            kbase_token_file=None,
            token="fake-kbase-token-for-tests",
        )
    except FileNotFoundError:
        pytest.skip(
            "cb_annotation_ontology_api data files not available "
            "(sibling repo not cloned)"
        )


def _write_mapping_parquet(path, rows):
    """Write a tiny mapping-version parquet with the M1 data-contract columns."""
    df = pd.DataFrame(
        rows,
        columns=[
            "function_hash",
            "function_description",
            "reaction_id",
            "score",
            "is_in_template",
            "is_in_core",
        ],
    )
    df.to_parquet(path, engine="pyarrow", index=False)
    return path


# fh1 -> two non-filtered reactions; fh2 -> one filtered (rxn00008 is in
# FilteredReactions.csv with reason "CI") + one non-filtered reaction.
BASE_ROWS = [
    ("fh1", "Function One", "rxn00001", 0.9, True, True),
    ("fh1", "Function One", "rxn00002", 0.5, False, True),
    ("fh2", "Function Two", "rxn00008", 0.7, True, False),
    ("fh2", "Function Two", "rxn00010", 0.3, True, True),
]

# A disjoint second mapping used to prove reload replaces rather than accumulates.
RELOAD_ROWS = [
    ("fh9", "Function Nine", "rxn00099", 0.1, True, True),
]


@pytest.fixture
def mapping_parquet(tmp_path):
    return _write_mapping_parquet(tmp_path / "mapping_v1.parquet", BASE_ROWS)


class TestLoadReactionMappingParquet:
    def test_load_builds_hash_and_description_indexes(self, annotation, mapping_parquet):
        result = annotation.load_reaction_mapping_parquet(mapping_parquet)
        assert result is annotation.reaction_mapping
        assert annotation.reaction_mapping["fh1"] == [
            ("rxn00001", 0.9, True, True),
            ("rxn00002", 0.5, False, True),
        ]
        assert annotation.reaction_mapping_by_description[
            annotation.convert_role_to_searchrole("Function One")
        ] == [
            ("rxn00001", 0.9, True, True),
            ("rxn00002", 0.5, False, True),
        ]


class TestTranslateFunctionToReactions:
    def test_lookup_by_function_hash_with_scores(self, annotation, mapping_parquet):
        annotation.load_reaction_mapping_parquet(mapping_parquet)
        result = annotation.translate_function_to_reactions(function_hash="fh1")
        assert result == [("rxn00001", 0.9), ("rxn00002", 0.5)]

    def test_lookup_by_function_hash_without_scores(self, annotation, mapping_parquet):
        annotation.load_reaction_mapping_parquet(mapping_parquet)
        result = annotation.translate_function_to_reactions(
            function_hash="fh1", with_scores=False
        )
        assert result == ["rxn00001", "rxn00002"]

    def test_lookup_by_description_resolves_via_normalized_index(
        self, annotation, mapping_parquet
    ):
        annotation.load_reaction_mapping_parquet(mapping_parquet)
        # Different case/spacing than the stored description; convert_role_to_searchrole
        # normalization should still resolve it to the same entries as function_hash="fh1".
        result = annotation.translate_function_to_reactions(description="  Function ONE ")
        assert result == [("rxn00001", 0.9), ("rxn00002", 0.5)]

    def test_msrxn_filter_excludes_filtered_reactions_csv_entries(
        self, annotation, mapping_parquet
    ):
        annotation.load_reaction_mapping_parquet(mapping_parquet)
        # rxn00008 is a real FilteredReactions.csv entry; default msrxn_filter=True excludes it.
        assert "rxn00008" in annotation.filtered_rxn
        assert annotation.msrxn_filter is True
        result = annotation.translate_function_to_reactions(function_hash="fh2")
        assert result == [("rxn00010", 0.3)]

    def test_msrxn_filter_off_includes_filtered_reactions(self, annotation, mapping_parquet):
        annotation.load_reaction_mapping_parquet(mapping_parquet)
        annotation.msrxn_filter = False
        result = annotation.translate_function_to_reactions(function_hash="fh2")
        assert result == [("rxn00008", 0.7), ("rxn00010", 0.3)]

    def test_missing_function_hash_returns_empty(self, annotation, mapping_parquet):
        annotation.load_reaction_mapping_parquet(mapping_parquet)
        assert annotation.translate_function_to_reactions(function_hash="does-not-exist") == []

    def test_missing_description_returns_empty(self, annotation, mapping_parquet):
        annotation.load_reaction_mapping_parquet(mapping_parquet)
        assert annotation.translate_function_to_reactions(description="no such role") == []

    def test_no_mapping_loaded_returns_empty(self, annotation):
        # Before load_reaction_mapping_parquet is ever called.
        assert annotation.translate_function_to_reactions(function_hash="fh1") == []
        assert annotation.translate_function_to_reactions(description="Function One") == []


class TestReloadIdempotency:
    def test_reloading_same_parquet_yields_same_result(self, annotation, mapping_parquet):
        annotation.load_reaction_mapping_parquet(mapping_parquet)
        first = annotation.translate_function_to_reactions(function_hash="fh1")
        annotation.load_reaction_mapping_parquet(mapping_parquet)
        second = annotation.translate_function_to_reactions(function_hash="fh1")
        assert first == second == [("rxn00001", 0.9), ("rxn00002", 0.5)]

    def test_reloading_new_parquet_replaces_not_accumulates(self, annotation, tmp_path):
        annotation.load_reaction_mapping_parquet(
            _write_mapping_parquet(tmp_path / "v1.parquet", BASE_ROWS)
        )
        assert annotation.translate_function_to_reactions(function_hash="fh1") != []

        annotation.load_reaction_mapping_parquet(
            _write_mapping_parquet(tmp_path / "v2.parquet", RELOAD_ROWS)
        )
        # Old key from the first load is gone; only the newly loaded key resolves.
        assert annotation.translate_function_to_reactions(function_hash="fh1") == []
        assert annotation.translate_function_to_reactions(function_hash="fh9") == [
            ("rxn00099", 0.1)
        ]


class TestExistingPathUnaffected:
    def test_translate_term_to_modelseed_still_works(self, annotation):
        """Sanity check that the additive path doesn't disturb the EC/bundled path."""
        result = annotation.translate_term_to_modelseed("MSRXN:rxn00001")
        assert isinstance(result, list)
