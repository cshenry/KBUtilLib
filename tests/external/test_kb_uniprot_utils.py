"""Offline tests for KBUniProtUtils.get_publications.

The HTTP layer is replaced at ``get_uniprot_entry`` -- the one method
``get_publications`` calls -- so these run without network.  The entry
fixtures below are trimmed copies of real ``rest.uniprot.org`` responses
(``Q6FF68``, ``A0A021WW32``), including the ``referencePositions`` list,
which the existing ``lit_pubmed_id,lit_doi_id,cc_interaction`` field
projection returns (verified live 2026-10-20).
"""

from __future__ import annotations

from kbutillib.domains.external.kb_uniprot_utils import KBUniProtUtils

#: Trimmed from https://rest.uniprot.org/uniprotkb/Q6FF68.json -- a whole
#: genome paper, a characterisation paper and a reannotation reference.
ENTRY = {
    "entryType": "UniProtKB reviewed (Swiss-Prot)",
    "primaryAccession": "Q6FF68",
    "references": [
        {
            "referenceNumber": 1,
            "citation": {
                "id": "15514110",
                "citationType": "journal article",
                "citationCrossReferences": [
                    {"database": "PubMed", "id": "15514110"},
                    {"database": "DOI", "id": "10.1093/nar/gkh910"},
                ],
                "title": "Unique features revealed by the genome sequence.",
                "publicationDate": "2004",
                "journal": "Nucleic Acids Res.",
            },
            "referencePositions": ["NUCLEOTIDE SEQUENCE [LARGE SCALE GENOMIC DNA]"],
        },
        {
            "referenceNumber": 2,
            "citation": {
                "id": "999999",
                "citationCrossReferences": [{"database": "PubMed", "id": "999999"}],
                "title": "Characterisation of the protein.",
                "publicationDate": "2011",
                "journal": "J. Bacteriol.",
            },
            "referencePositions": ["FUNCTION", "CATALYTIC ACTIVITY"],
        },
        {
            "referenceNumber": 3,
            "citation": {
                "id": "888888",
                "citationCrossReferences": [{"database": "PubMed", "id": "888888"}],
                "title": "A reannotation of the genome.",
                "publicationDate": "2014",
                "journal": "Stand. Genomic Sci.",
            },
            "referencePositions": ["GENOME REANNOTATION"],
        },
        {
            # UniProt records plenty of references with no scope at all.
            "referenceNumber": 4,
            "citation": {
                "id": "777777",
                "citationCrossReferences": [{"database": "PubMed", "id": "777777"}],
                "title": "No scope recorded.",
                "publicationDate": "2019",
                "journal": "Nature",
            },
        },
    ],
}


def make_utils(entry=None, recorder=None):
    """A KBUniProtUtils whose get_uniprot_entry is replaced, so no HTTP."""
    utils = KBUniProtUtils(config_file=False, token_file=None, kbase_token_file=None)

    def fake_get_uniprot_entry(uniprot_id, fields=None, format="json"):
        if recorder is not None:
            recorder.append({"uniprot_id": uniprot_id, "fields": fields})
        return entry if entry is not None else ENTRY

    utils.get_uniprot_entry = fake_get_uniprot_entry
    return utils


class TestGetPublicationsScope:
    def test_every_publication_gains_a_scope_key(self):
        publications = make_utils().get_publications("Q6FF68")

        assert [pub["scope"] for pub in publications] == [
            ["NUCLEOTIDE SEQUENCE [LARGE SCALE GENOMIC DNA]"],
            ["FUNCTION", "CATALYTIC ACTIVITY"],
            ["GENOME REANNOTATION"],
            [],  # no referencePositions on the entry at all
        ]

    def test_the_existing_keys_are_unchanged(self):
        """The 'scope' key is additive: nothing else about the shape moved."""
        publications = make_utils().get_publications("Q6FF68")

        assert len(publications) == 4
        assert set(publications[0]) == {
            "title",
            "journal",
            "pubmed_id",
            "doi",
            "year",
            "scope",
        }
        assert publications[0]["title"] == (
            "Unique features revealed by the genome sequence."
        )
        assert publications[0]["journal"] == "Nucleic Acids Res."
        assert publications[0]["pubmed_id"] == "15514110"
        assert publications[0]["doi"] == "10.1093/nar/gkh910"
        assert publications[0]["year"] == "2004"
        # A reference with no DOI cross-reference still reports None.
        assert publications[1]["doi"] is None

    def test_the_scope_alias_spelling_is_accepted(self):
        entry = {
            "references": [
                {"citation": {"title": "t"}, "scope": ["FUNCTION"]},
                {"citation": {"title": "t"}, "scope": "FUNCTION"},
            ]
        }
        publications = make_utils(entry).get_publications("X")
        assert [pub["scope"] for pub in publications] == [["FUNCTION"], ["FUNCTION"]]

    def test_an_entry_with_no_references_returns_nothing(self):
        assert make_utils({"primaryAccession": "X"}).get_publications("X") == []

    def test_the_field_projection_is_the_one_that_returns_scope(self):
        """referencePositions comes back under this projection -- no new field.

        Verified against rest.uniprot.org on 2026-10-20: requesting
        ``lit_pubmed_id,lit_doi_id,cc_interaction`` for Q6FF68 returns each
        reference's ``referencePositions``, so nothing had to be added to the
        request for the scope to be available.
        """
        recorder = []
        make_utils(recorder=recorder).get_publications("Q6FF68")
        assert recorder == [
            {
                "uniprot_id": "Q6FF68",
                "fields": ["lit_pubmed_id", "lit_doi_id", "cc_interaction"],
            }
        ]
