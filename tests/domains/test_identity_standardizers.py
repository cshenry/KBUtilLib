"""Tests for domains.identity.standardizers — the KBDL Clearinghouse's
canonical entity standardization and hashing module.

These are fixed-vector tests: every rule transcribed from GAA's production
behavior is pinned to a concrete input/output pair, plus the awkward edge
cases called out in the task's success criteria (non-ASCII casing, single
trailing period, protein '*' preservation, I/L non-normalization, DNA
non-reverse-complement, IUPAC ambiguity codes, contig sort-before-join, and
canonical_payload key-order independence).
"""
from __future__ import annotations

import hashlib

import pytest

# ---------------------------------------------------------------------------
# Import path + version
# ---------------------------------------------------------------------------


def test_public_names_importable_from_canonical_path() -> None:
    """The five public names are importable from kbutillib.domains.identity."""
    from kbutillib.domains.identity import (  # noqa: PLC0415
        STANDARDIZER_VERSION,
        canonical_payload,
        content_hash,
        entity_hash,
        standardize,
    )

    assert STANDARDIZER_VERSION == "1.0"
    assert callable(standardize)
    assert callable(entity_hash)
    assert callable(canonical_payload)
    assert callable(content_hash)


def test_two_independent_import_paths_agree_on_hash() -> None:
    """Importing the module two different ways yields the same hash for the same input."""
    import kbutillib.domains.identity as pkg_path  # noqa: PLC0415
    from kbutillib.domains.identity.standardizers import (  # noqa: PLC0415
        entity_hash as submodule_entity_hash,
    )

    raw = "Example function description."
    assert pkg_path.entity_hash("function", raw) == submodule_entity_hash(
        "function", raw
    )


def test_unknown_entity_type_raises_value_error_naming_value_and_supported_types() -> None:
    from kbutillib.domains.identity import standardize  # noqa: PLC0415

    with pytest.raises(ValueError, match="bogus_type"):
        standardize("bogus_type", "x")


# ---------------------------------------------------------------------------
# function
# ---------------------------------------------------------------------------


def test_function_nfc_normalizes_and_strips_and_collapses_whitespace() -> None:
    from kbutillib.domains.identity import standardize  # noqa: PLC0415

    assert (
        standardize("function", "  ATP   synthase\tsubunit  \n alpha  ")
        == "atp synthase subunit alpha"
    )


def test_function_lowercases_ascii_letters_only_not_non_ascii() -> None:
    """A non-ASCII letter (e.g. 'É', 'ñ') is NOT lowercased — no str.lower()."""
    from kbutillib.domains.identity import standardize  # noqa: PLC0415

    # 'É' (U+00C9) must remain uppercase; ASCII 'PROTEIN' must fold to lowercase.
    assert standardize("function", "É PROTEIN") == "É protein"


def test_function_strips_exactly_one_trailing_period() -> None:
    """'x..' -> 'x.' (one period stripped, not two)."""
    from kbutillib.domains.identity import standardize  # noqa: PLC0415

    assert standardize("function", "x..") == "x."
    assert standardize("function", "x.") == "x"
    assert standardize("function", "x") == "x"


def test_function_keeps_other_punctuation() -> None:
    from kbutillib.domains.identity import standardize  # noqa: PLC0415

    assert standardize("function", "3-oxoacyl-[acp] reductase") == (
        "3-oxoacyl-[acp] reductase"
    )


def test_function_hash_is_sha256_of_canonical_utf8() -> None:
    from kbutillib.domains.identity import entity_hash, standardize  # noqa: PLC0415

    raw = "  Hydrolase Activity.. "
    canonical = standardize("function", raw)
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert entity_hash("function", raw) == expected


# ---------------------------------------------------------------------------
# protein
# ---------------------------------------------------------------------------


def test_protein_strips_fasta_header_and_whitespace_then_uppercases() -> None:
    from kbutillib.domains.identity import standardize  # noqa: PLC0415

    raw = ">sp|P0A6F5|gene desc\nmkvl gklv\naakl\n"
    assert standardize("protein", raw) == "MKVLGKLVAAKL"


def test_protein_preserves_trailing_stop_marker() -> None:
    """A trailing '*' on a protein is NOT stripped."""
    from kbutillib.domains.identity import standardize  # noqa: PLC0415

    assert standardize("protein", "MKVL*") == "MKVL*"


def test_protein_does_not_normalize_i_and_l() -> None:
    from kbutillib.domains.identity import standardize  # noqa: PLC0415

    assert standardize("protein", "ILIL") == "ILIL"
    assert standardize("protein", "ilil") == "ILIL"


def test_protein_preserves_iupac_ambiguity_codes() -> None:
    """IUPAC ambiguity codes B, Z, J, X, U, O survive uppercasing."""
    from kbutillib.domains.identity import standardize  # noqa: PLC0415

    assert standardize("protein", "bzjxuo") == "BZJXUO"


def test_protein_hash_over_residue_letters_only_no_header_no_newlines() -> None:
    from kbutillib.domains.identity import entity_hash  # noqa: PLC0415

    with_header = ">header line\nMKVL\n"
    without_header = "MKVL"
    assert entity_hash("protein", with_header) == entity_hash(
        "protein", without_header
    )


# ---------------------------------------------------------------------------
# gene_dna
# ---------------------------------------------------------------------------


def test_gene_dna_strips_header_whitespace_then_uppercases() -> None:
    from kbutillib.domains.identity import standardize  # noqa: PLC0415

    raw = ">contig1\nacgt\nacgt\n"
    assert standardize("gene_dna", raw) == "ACGTACGT"


def test_gene_dna_preserves_characters_outside_acgtn() -> None:
    """Characters outside ACGTN (e.g. ambiguity codes) are preserved, not rejected."""
    from kbutillib.domains.identity import standardize  # noqa: PLC0415

    assert standardize("gene_dna", "acgtnryw") == "ACGTNRYW"


def test_gene_dna_is_not_reverse_complement_folded() -> None:
    """The canonical form of a sequence and its reverse complement must differ."""
    from kbutillib.domains.identity import standardize  # noqa: PLC0415

    forward = "AAGT"
    revcomp = forward[::-1].translate(str.maketrans("ACGT", "TGCA"))
    assert standardize("gene_dna", forward) != standardize("gene_dna", revcomp)


# ---------------------------------------------------------------------------
# genome
# ---------------------------------------------------------------------------


def test_genome_sorts_canonical_contigs_before_joining_with_pipe() -> None:
    from kbutillib.domains.identity import standardize  # noqa: PLC0415

    # Contigs given out of sorted order; canonical form must sort them first.
    contigs = ["ttt", "aaaa", "gg"]
    assert standardize("genome", contigs) == "AAAA|GG|TTT"


def test_genome_join_order_independent_of_input_order() -> None:
    """Two assemblies with the same contigs in a different order hash identically."""
    from kbutillib.domains.identity import entity_hash  # noqa: PLC0415

    assert entity_hash("genome", ["aaaa", "ttt", "gg"]) == entity_hash(
        "genome", ["gg", "ttt", "aaaa"]
    )


def test_genome_contigs_canonicalized_with_dna_rule_before_sorting() -> None:
    from kbutillib.domains.identity import standardize  # noqa: PLC0415

    contigs = [">c2\nttt\n", ">c1\naaaa\n"]
    assert standardize("genome", contigs) == "AAAA|TTT"


# ---------------------------------------------------------------------------
# ontology_term
# ---------------------------------------------------------------------------


def test_ontology_term_uppercases_namespace_preserves_term_body_verbatim() -> None:
    from kbutillib.domains.identity import standardize  # noqa: PLC0415

    assert standardize("ontology_term", "  go:GO:0008150  ") == "GO:GO:0008150"


def test_ontology_term_no_colon_is_only_whitespace_stripped() -> None:
    from kbutillib.domains.identity import standardize  # noqa: PLC0415

    assert standardize("ontology_term", "  BARE_TERM  ") == "BARE_TERM"


def test_ontology_term_hash_is_sha256_of_canonical_string() -> None:
    from kbutillib.domains.identity import entity_hash, standardize  # noqa: PLC0415

    raw = "ko:K00001"
    canonical = standardize("ontology_term", raw)
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert entity_hash("ontology_term", raw) == expected


# ---------------------------------------------------------------------------
# canonical_payload / content_hash
# ---------------------------------------------------------------------------


def test_canonical_payload_identical_bytes_regardless_of_key_insertion_order() -> None:
    from kbutillib.domains.identity import canonical_payload  # noqa: PLC0415

    a = {"alpha": 1, "beta": 2, "gamma": {"x": 1, "y": 2}}
    b = {"gamma": {"y": 2, "x": 1}, "beta": 2, "alpha": 1}
    assert canonical_payload(a) == canonical_payload(b)


def test_canonical_payload_no_insignificant_whitespace_and_sorted_keys() -> None:
    from kbutillib.domains.identity import canonical_payload  # noqa: PLC0415

    payload = canonical_payload({"b": 1, "a": 2})
    assert payload == b'{"a":2,"b":1}'


def test_canonical_payload_ensure_ascii_false_preserves_non_ascii() -> None:
    from kbutillib.domains.identity import canonical_payload  # noqa: PLC0415

    payload = canonical_payload({"name": "café"})
    assert "café".encode("utf-8") in payload
    assert b"\\u" not in payload


def test_content_hash_matches_sha256_of_canonical_payload() -> None:
    from kbutillib.domains.identity import (  # noqa: PLC0415
        canonical_payload,
        content_hash,
    )

    obj = {"z": [3, 2, 1], "a": None}
    expected = hashlib.sha256(canonical_payload(obj)).hexdigest()
    assert content_hash(obj) == expected


def test_content_hash_identical_for_dicts_differing_only_in_key_order() -> None:
    from kbutillib.domains.identity import content_hash  # noqa: PLC0415

    a = {"one": 1, "two": 2}
    b = {"two": 2, "one": 1}
    assert content_hash(a) == content_hash(b)
