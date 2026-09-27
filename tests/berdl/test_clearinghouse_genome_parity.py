"""Genome FASTA-rendering invariance, driven by the shared parity fixture.

The property this PRD exists to guarantee: the common renderings of ONE
assembly -- plain (unwrapped), 60-column wrapped, trailing newline, and
header-bearing -- all canonicalise to a single identical genome
``entity_hash``. Line wrapping in particular is a display choice a source
may make freely; it must never fork a genome's identity.

These tests read the genome case out of
:mod:`kbutillib.domains.kbase.berdl.clearinghouse_parity_fixture` (the same
fixture the DuckDB derivation tests and the in-pod OP3 parity check share)
so the assertion and the fixture cannot drift apart.
"""

from __future__ import annotations

from kbutillib.domains.identity.standardizers import genome_hash_from_fasta
from kbutillib.domains.kbase.berdl.clearinghouse_parity_fixture import (
    GENOME_ASSEMBLY_HASH,
    GENOME_FASTA_INVARIANCE,
    GENOME_RENDERINGS,
    PARITY_CASES,
)


def test_fixture_contains_a_genome_parity_case():
    """The parity fixture is no longer protein-only: at least one genome case
    exists and it targets exactly the genome entity_type."""
    genome_cases = [c for c in PARITY_CASES if c.entity_type == "genome"]
    assert genome_cases, "parity fixture has no genome ParityCase"
    assert GENOME_FASTA_INVARIANCE in genome_cases


def test_four_renderings_are_present():
    """All four renderings named by the PRD are covered."""
    assert set(GENOME_RENDERINGS) == {
        "plain",
        "wrapped_60",
        "trailing_newline",
        "header_bearing",
    }


def test_all_renderings_share_one_digest():
    """plain, 60-column-wrapped, trailing-newline and header-bearing
    renderings of one assembly produce one identical genome digest."""
    digests = {
        label: genome_hash_from_fasta(source)
        for label, source in GENOME_RENDERINGS.items()
    }
    assert set(digests.values()) == {GENOME_ASSEMBLY_HASH}, (
        f"renderings disagree on the genome digest: {digests!r}"
    )


def test_wrapped_equals_unwrapped():
    """The load-bearing assertion: a 60-column-wrapped rendering hashes
    identically to the unwrapped (plain) rendering. Line wrapping is a
    display choice and must never change a genome's identity."""
    assert genome_hash_from_fasta(
        GENOME_RENDERINGS["wrapped_60"]
    ) == genome_hash_from_fasta(GENOME_RENDERINGS["plain"])


def test_genome_case_rows_all_carry_the_shared_hash():
    """Every fixture row in the genome case shares the one assembly digest,
    so a derivation reader sees the four renderings as the same entity."""
    assert GENOME_FASTA_INVARIANCE.entity_hash == GENOME_ASSEMBLY_HASH
    assert {row["entity_hash"] for row in GENOME_FASTA_INVARIANCE.rows} == {
        GENOME_ASSEMBLY_HASH
    }
    assert {row["entity_type"] for row in GENOME_FASTA_INVARIANCE.rows} == {"genome"}
