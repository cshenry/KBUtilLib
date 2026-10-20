"""Gated live test for EuropePMCClient against the real Europe PMC API.

This is the only check that the recorded fixtures in
``tests/literature/fixtures/`` still match the shape Europe PMC actually
returns.  It makes real HTTP calls to a public, token-free service, so it is
skipped unless opted into explicitly::

    KBU_LIVE_TESTS=1 pytest tests/literature/test_europepmc_live.py -v

The query used is a published genome paper (Acinetobacter sp. ADP1), so
nothing sensitive leaves the machine.  The cache is pointed at a tmp path so
a live run never pollutes the user's real literature cache.
"""

from __future__ import annotations

import os

import pytest

from kbutillib.domains.literature.cache import LiteratureCache
from kbutillib.domains.literature.europepmc import EuropePMCClient

pytestmark = pytest.mark.skipif(
    os.environ.get("KBU_LIVE_TESTS") != "1",
    reason="Europe PMC live tests disabled (set KBU_LIVE_TESTS=1 to run)",
)

# A stable, well-known record: the Acinetobacter sp. ADP1 genome paper.
ADP1_GENOME_PMID = "15210978"


@pytest.fixture()
def live_client(tmp_path):
    """A real client whose cache is isolated to the test's tmp directory."""
    return EuropePMCClient(
        cache=LiteratureCache(tmp_path / "live.sqlite"),
        config={"gene_literature": {"europepmc_rps": 2}},
        config_file=False,
        token_file=None,
        kbase_token_file=None,
    )


def test_live_search_returns_populated_hits(live_client):
    hits = live_client.search(
        '"Acinetobacter baylyi" AND "ADP1"', page_size=10, max_results=10
    )
    assert hits, "live Europe PMC search returned no hits"
    assert any(hit["title"] for hit in hits)
    assert any(hit["pmid"] or hit["pmcid"] or hit["doi"] for hit in hits)
    assert all(isinstance(hit["year"], (int, type(None))) for hit in hits)


def test_live_resolve_round_trips_a_known_pmid(live_client):
    record = live_client.resolve(ADP1_GENOME_PMID)
    assert record is not None
    assert record["canonical_id"] == f"PMID:{ADP1_GENOME_PMID}"
    assert record["pmid"] == ADP1_GENOME_PMID
    assert "Acinetobacter" in (record["title"] or "")
